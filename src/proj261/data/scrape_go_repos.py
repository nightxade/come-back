#!/usr/bin/env python3
"""Scrape popular Go repositories from GitHub, clone them, and compile binaries
at multiple optimization levels for decompiler training data."""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from proj261.util import DATA_DIR, REPOS_DIR, BINARIES_DIR, METADATA_PATH

from tqdm import tqdm

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"

# Repos larger than this (KB on GitHub) are skipped to avoid long clones.
MAX_REPO_SIZE_KB = 500_000  # 500 MB

CLONE_TIMEOUT = 120   # seconds
BUILD_TIMEOUT = 300   # seconds

BUILD_VARIANTS = {
    "default": [],
    "debug": ["-gcflags=-N -l"],
    "stripped": ["-ldflags=-s -w"],
}


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def gh_api(endpoint: str, params: dict | None = None) -> dict:
    """Call the GitHub API via the `gh` CLI (uses user's auth)."""
    if params:
        from urllib.parse import urlencode
        endpoint = f"{endpoint}?{urlencode(params)}"
    cmd = ["gh", "api", endpoint]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def run(cmd: list[str], timeout: int = 120, env: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a command with timeout, returning the CompletedProcess."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=merged_env, cwd=cwd,
    )


def load_metadata() -> dict:
    """Load existing metadata or return a fresh structure."""
    if METADATA_PATH.exists():
        return json.loads(METADATA_PATH.read_text())
    return {
        "scraped_at": None,
        "go_version": None,
        "repos": {},
    }


def save_metadata(meta: dict) -> None:
    METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    METADATA_PATH.write_text(json.dumps(meta, indent=2) + "\n")


# --------------------------------------------------------------------------- #
#  Phase 1 – Discover repos
# --------------------------------------------------------------------------- #

def discover_repos(max_repos: int = 200) -> list[dict]:
    """Fetch top Go repos from GitHub, sorted by stars."""
    repos: list[dict] = []
    per_page = min(max_repos, 100)
    page = 1

    print(f"Discovering top {max_repos} Go repositories...")
    while len(repos) < max_repos:
        data = gh_api(
            "search/repositories",
            {
                "q": "language:go stars:>500 archived:false fork:false",
                "sort": "stars",
                "order": "desc",
                "per_page": str(per_page),
                "page": str(page),
            },
        )
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            if item.get("size", 0) > MAX_REPO_SIZE_KB:
                continue
            repos.append({
                "full_name": item["full_name"],
                "stars": item["stargazers_count"],
                "url": item["html_url"],
                "clone_url": item["clone_url"],
                "size_kb": item["size"],
                "default_branch": item["default_branch"],
            })
            if len(repos) >= max_repos:
                break
        page += 1
        time.sleep(1)  # be polite to the API

    print(f"  Found {len(repos)} repos.")
    return repos


# --------------------------------------------------------------------------- #
#  Phase 2 – Clone repos
# --------------------------------------------------------------------------- #

def clone_repos(repos: list[dict], meta: dict) -> dict:
    """Shallow-clone each repo into data/repos/."""
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nCloning {len(repos)} repositories...")

    for repo in tqdm(repos, desc="Cloning", unit="repo"):
        name = repo["full_name"]
        sname = safe_name(name)
        dest = REPOS_DIR / sname

        entry = meta["repos"].setdefault(name, {
            "stars": repo["stars"],
            "url": repo["url"],
            "clone_url": repo["clone_url"],
            "cloned": False,
            "clone_error": None,
            "binaries": {},
            "build_errors": {},
            "compiled_at": None,
        })

        if entry.get("cloned") and dest.exists():
            continue  # already cloned

        try:
            result = run(
                ["git", "clone", "--depth=1", repo["clone_url"], str(dest)],
                timeout=CLONE_TIMEOUT,
            )
            if result.returncode == 0:
                entry["cloned"] = True
                entry["clone_error"] = None
            else:
                entry["clone_error"] = result.stderr.strip()[:500]
        except subprocess.TimeoutExpired:
            entry["clone_error"] = "clone timed out"
        except Exception as e:
            entry["clone_error"] = str(e)[:500]

        save_metadata(meta)  # incremental save

    return meta


# --------------------------------------------------------------------------- #
#  Phase 3 – Compile binaries
# --------------------------------------------------------------------------- #

def find_main_packages(repo_dir: str) -> list[str]:
    """Use `go list` to find all main packages in a repo."""
    try:
        # Download dependencies first (can be slow on first run)
        run(["go", "mod", "download"], timeout=180, cwd=repo_dir, env={"CGO_ENABLED": "0"})
        result = run(
            ["go", "list", "-e", "-f", '{{if eq .Name "main"}}{{.ImportPath}}{{end}}', "./..."],
            timeout=120,
            cwd=repo_dir,
            env={"CGO_ENABLED": "0"},
        )
        # go list may exit non-zero due to errors in some packages,
        # but still outputs valid main packages on stdout — parse regardless.
        return [line for line in result.stdout.strip().splitlines() if line]
    except Exception:
        return []


def compile_repos(meta: dict) -> dict:
    """Compile all cloned repos with each build variant."""
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)

    cloned = {name: info for name, info in meta["repos"].items() if info.get("cloned")}
    print(f"\nCompiling {len(cloned)} repositories (3 variants each)...")

    build_env = {"CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64"}

    for name, info in tqdm(cloned.items(), desc="Compiling", unit="repo"):
        sname = safe_name(name)
        repo_dir = str(REPOS_DIR / sname)

        if not Path(repo_dir).exists():
            continue

        # Skip if all variants already compiled
        if info.get("compiled_at") and all(v in info.get("binaries", {}) for v in BUILD_VARIANTS):
            continue

        # Discover main packages
        main_pkgs = find_main_packages(repo_dir)
        if not main_pkgs:
            info["build_errors"]["_discover"] = "no main packages found"
            save_metadata(meta)
            continue

        for variant, extra_flags in BUILD_VARIANTS.items():
            variant_dir = BINARIES_DIR / sname / variant
            variant_dir.mkdir(parents=True, exist_ok=True)

            built_binaries: list[str] = []

            for pkg in main_pkgs:
                # Use last component of import path as binary name
                bin_name = pkg.rsplit("/", 1)[-1]
                out_path = variant_dir / bin_name

                cmd = ["go", "build"] + extra_flags + ["-o", str(out_path), pkg]

                try:
                    result = run(cmd, timeout=BUILD_TIMEOUT, env=build_env, cwd=repo_dir)
                    if result.returncode == 0 and out_path.exists():
                        built_binaries.append(bin_name)
                    else:
                        err_key = f"{variant}/{pkg}"
                        err_msg = result.stderr.strip() or result.stdout.strip()
                        info["build_errors"][err_key] = err_msg[:300]
                except subprocess.TimeoutExpired:
                    info["build_errors"][f"{variant}/{pkg}"] = "build timed out"
                except Exception as e:
                    info["build_errors"][f"{variant}/{pkg}"] = str(e)[:300]

            info.setdefault("binaries", {})[variant] = built_binaries

        info["compiled_at"] = datetime.now(timezone.utc).isoformat()
        save_metadata(meta)

    return meta


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def get_go_version() -> str:
    try:
        result = run(["go", "version"], timeout=10)
        return result.stdout.strip()
    except Exception:
        return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Scrape and compile Go repos for decompiler training.")
    parser.add_argument("--max-repos", type=int, default=200, help="Max repos to scrape (default: 200)")
    parser.add_argument("--discover-only", action="store_true", help="Only discover repos, don't clone/compile")
    parser.add_argument("--compile-only", action="store_true", help="Only compile already-cloned repos")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    meta = load_metadata()
    meta["go_version"] = get_go_version()

    if not args.compile_only:
        # Phase 1: Discover
        repos = discover_repos(args.max_repos)
        meta["scraped_at"] = datetime.now(timezone.utc).isoformat()

        # Seed metadata with discovered repos
        for repo in repos:
            meta["repos"].setdefault(repo["full_name"], {
                "stars": repo["stars"],
                "url": repo["url"],
                "clone_url": repo["clone_url"],
                "cloned": False,
                "clone_error": None,
                "binaries": {},
                "build_errors": {},
                "compiled_at": None,
            })
        save_metadata(meta)

        if args.discover_only:
            print(f"\nDiscovery complete. {len(repos)} repos saved to {METADATA_PATH}")
            return

        # Phase 2: Clone
        meta = clone_repos(repos, meta)

    # Phase 3: Compile
    meta = compile_repos(meta)

    # Summary
    total = len(meta["repos"])
    cloned = sum(1 for r in meta["repos"].values() if r.get("cloned"))
    compiled = sum(1 for r in meta["repos"].values() if r.get("compiled_at"))
    has_bins = sum(1 for r in meta["repos"].values() if any(r.get("binaries", {}).get(v) for v in BUILD_VARIANTS))

    print(f"\n{'='*60}")
    print(f"  Total repos discovered:  {total}")
    print(f"  Successfully cloned:     {cloned}")
    print(f"  Compiled (attempted):    {compiled}")
    print(f"  With at least 1 binary:  {has_bins}")
    print(f"  Metadata: {METADATA_PATH}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
