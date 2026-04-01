#!/usr/bin/env python3
"""Map compiled Go binaries to their source files using `go list -deps -json`.

Reads metadata.json to find successfully compiled binaries, then uses the Go
toolchain to extract the full transitive dependency tree for each binary's
main package.  Produces data/source_map.json with per-binary source listings
categorised as repo-local, stdlib, or external module files.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
REPOS_DIR = DATA_DIR / "repos"
BINARIES_DIR = DATA_DIR / "binaries"
METADATA_PATH = DATA_DIR / "metadata.json"
SOURCE_MAP_PATH = DATA_DIR / "source_map.json"

BUILD_ENV = {"CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64"}

LIST_TIMEOUT = 180  # seconds per go list invocation


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def safe_name(full_name: str) -> str:
    """Convert 'owner/repo' to 'owner__repo' for filesystem use."""
    return full_name.replace("/", "__")


def run(cmd: list[str], timeout: int = 120, cwd: str | None = None) -> subprocess.CompletedProcess:
    merged_env = {**os.environ, **BUILD_ENV}
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=merged_env, cwd=cwd,
    )


def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


def load_source_map() -> dict:
    if SOURCE_MAP_PATH.exists():
        return json.loads(SOURCE_MAP_PATH.read_text())
    return {"mappings": []}


def save_source_map(smap: dict) -> None:
    SOURCE_MAP_PATH.write_text(json.dumps(smap, indent=2) + "\n")


def get_goroot() -> str:
    result = run(["go", "env", "GOROOT"], timeout=10)
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
#  Core: discover main packages in a repo
# --------------------------------------------------------------------------- #

def find_main_packages(repo_dir: str) -> dict[str, str]:
    """Return {binary_name: import_path} for every main package in repo_dir."""
    try:
        result = run(
            ["go", "list", "-e", "-f", '{{if eq .Name "main"}}{{.ImportPath}}{{end}}', "./..."],
            timeout=LIST_TIMEOUT,
            cwd=repo_dir,
        )
        mapping = {}
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            bin_name = line.rsplit("/", 1)[-1]
            mapping[bin_name] = line
        return mapping
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
#  Core: parse `go list -deps -json` stream
# --------------------------------------------------------------------------- #

def parse_json_stream(text: str) -> list[dict]:
    """Parse concatenated JSON objects from `go list -deps -json` output."""
    decoder = json.JSONDecoder()
    objects = []
    idx = 0
    text = text.strip()
    while idx < len(text):
        # Skip whitespace between objects
        while idx < len(text) and text[idx] in " \t\n\r":
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            objects.append(obj)
            idx = end
        except json.JSONDecodeError:
            break
    return objects


def collect_source_files(pkg: dict) -> list[str]:
    """Collect all source file basenames from a package JSON object."""
    files = []
    for key in ("GoFiles", "CgoFiles", "SFiles", "HFiles", "CFiles"):
        files.extend(pkg.get(key, []))
    return files


def map_binary(repo_dir: str, import_path: str, goroot: str) -> dict | None:
    """Run `go list -deps -json <import_path>` and categorise source files.

    Returns a dict with keys: main_package, source_files, package_count, file_counts.
    Returns None on failure.
    """
    try:
        result = run(
            ["go", "list", "-deps", "-json", import_path],
            timeout=LIST_TIMEOUT,
            cwd=repo_dir,
        )
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

    if result.returncode != 0 and not result.stdout.strip():
        return None

    packages = parse_json_stream(result.stdout)
    if not packages:
        return None

    repo_dir_abs = str(Path(repo_dir).resolve())
    goroot_src = str(Path(goroot) / "src")

    repo_files = []
    stdlib_files = []
    external_files = []

    for pkg in packages:
        pkg_dir = pkg.get("Dir", "")
        is_stdlib = pkg.get("Standard", False)
        src_files = collect_source_files(pkg)

        if not src_files or not pkg_dir:
            continue

        if is_stdlib:
            # Strip $GOROOT/src/ prefix
            for f in src_files:
                abs_path = os.path.join(pkg_dir, f)
                if abs_path.startswith(goroot_src):
                    rel = os.path.relpath(abs_path, goroot_src)
                else:
                    rel = os.path.join(pkg.get("ImportPath", ""), f)
                stdlib_files.append(rel)

        elif pkg_dir.startswith(repo_dir_abs):
            # Repo-local file: strip repo dir prefix
            for f in src_files:
                abs_path = os.path.join(pkg_dir, f)
                rel = os.path.relpath(abs_path, repo_dir_abs)
                repo_files.append(rel)

        else:
            # External dependency: use module@version/relative_path
            module = pkg.get("Module")
            if module and module.get("Path") and module.get("Version"):
                mod_prefix = f"{module['Path']}@{module['Version']}"
                # pkg_dir is like /home/user/go/pkg/mod/github.com/foo/bar@v1.2.3/sub
                # We need the path relative to the module root in the mod cache
                mod_dir = pkg.get("Module", {}).get("Dir", "")
                for f in src_files:
                    abs_path = os.path.join(pkg_dir, f)
                    if mod_dir and abs_path.startswith(mod_dir):
                        rel = os.path.relpath(abs_path, mod_dir)
                        external_files.append(f"{mod_prefix}/{rel}")
                    else:
                        external_files.append(f"{mod_prefix}/{f}")
            else:
                # Fallback: just use import path
                imp = pkg.get("ImportPath", "unknown")
                for f in src_files:
                    external_files.append(f"{imp}/{f}")

    return {
        "main_package": import_path,
        "source_files": {
            "repo": sorted(repo_files),
            "stdlib": sorted(stdlib_files),
            "external": sorted(external_files),
        },
        "package_count": len(packages),
        "file_counts": {
            "repo": len(repo_files),
            "stdlib": len(stdlib_files),
            "external": len(external_files),
        },
    }


# --------------------------------------------------------------------------- #
#  Main driver
# --------------------------------------------------------------------------- #

def existing_keys(smap: dict) -> set[str]:
    """Return set of (repo, binary, variant) keys already mapped."""
    keys = set()
    for m in smap["mappings"]:
        keys.add((m["repo"], m["binary"], m["variant"]))
    return keys


def process_repo(repo_name: str, repo_info: dict, smap: dict, goroot: str, done: set[str]) -> int:
    """Map all binaries for a single repo. Returns count of new mappings added."""
    sname = safe_name(repo_name)
    repo_dir = str(REPOS_DIR / sname)

    if not Path(repo_dir).exists():
        return 0

    binaries_by_variant = repo_info.get("binaries", {})
    # Only process "default" variant — all variants share the same source deps
    # but we map each variant for completeness
    has_any = any(bool(bins) for bins in binaries_by_variant.values())
    if not has_any:
        return 0

    # Discover main packages once
    main_pkgs = find_main_packages(repo_dir)
    if not main_pkgs:
        return 0

    added = 0

    for variant, bin_list in binaries_by_variant.items():
        for bin_name in bin_list:
            key = (repo_name, bin_name, variant)
            if key in done:
                continue

            # Find the import path for this binary
            import_path = main_pkgs.get(bin_name)
            if not import_path:
                continue

            result = map_binary(repo_dir, import_path, goroot)
            if result is None:
                continue

            binary_path = f"data/binaries/{sname}/{variant}/{bin_name}"

            entry = {
                "repo": repo_name,
                "binary": bin_name,
                "variant": variant,
                "binary_path": binary_path,
                **result,
            }

            smap["mappings"].append(entry)
            done.add(key)
            added += 1

    return added


def main():
    parser = argparse.ArgumentParser(
        description="Map compiled Go binaries to their source files.",
    )
    parser.add_argument("--repo", type=str, default=None, help="Map a specific repo only (e.g. ollama/ollama)")
    args = parser.parse_args()

    meta = load_metadata()
    smap = load_source_map()
    done = existing_keys(smap)
    goroot = get_goroot()

    if args.repo:
        repos_to_process = {args.repo: meta["repos"].get(args.repo)}
        if repos_to_process[args.repo] is None:
            print(f"Error: repo '{args.repo}' not found in metadata.json", file=sys.stderr)
            sys.exit(1)
    else:
        repos_to_process = {
            name: info for name, info in meta["repos"].items()
            if info.get("cloned") and info.get("compiled_at")
        }

    total_added = 0
    for repo_name, repo_info in tqdm(repos_to_process.items(), desc="Mapping sources", unit="repo"):
        added = process_repo(repo_name, repo_info, smap, goroot, done)
        if added:
            total_added += added
            save_source_map(smap)  # incremental save after each repo

    # Summary
    total_mappings = len(smap["mappings"])
    repos_mapped = len({m["repo"] for m in smap["mappings"]})
    print(f"\n{'='*60}")
    print(f"  New mappings added:    {total_added}")
    print(f"  Total mappings:        {total_mappings}")
    print(f"  Repos with mappings:   {repos_mapped}")
    print(f"  Output: {SOURCE_MAP_PATH}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
