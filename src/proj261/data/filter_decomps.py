"""Filter Ghidra decompilations to keep only user-implemented functions.

Extracts the Go module path from each compiled binary via `go version -m`,
then strips all functions from the decomp whose names don't start with
that module path (or `main.`).  This removes stdlib, runtime, vendored,
and external dependency functions — typically 80-99% of the file.

Writes filtered output to data/decomps_filtered/ mirroring the layout of
data/decomps/.
"""

import argparse
import json
import re
import subprocess
from pathlib import Path

from proj261.util import (
    BINARIES_DIR,
    DECOMPS_DIR,
    FILTERED_DECOMPS_DIR,
    METADATA_PATH,
    safe_name,
)
from tqdm import tqdm


def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


def get_module_path(binary_path: Path) -> str | None:
    """Extract the Go module path from a compiled binary via `go version -m`."""
    try:
        r = subprocess.run(
            ["go", "version", "-m", str(binary_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "mod":
            return parts[1]
    return None


def is_user_func(name: str, module_path: str) -> bool:
    """Check if a function name belongs to the user's module.

    Enforces a path boundary (/ or .) after the module path to avoid
    matching external deps that share a name prefix, e.g.
    github.com/hashicorp/terraform-plugin-log would NOT match
    module github.com/hashicorp/terraform.
    """
    if name.startswith("main."):
        return True
    return name.startswith(module_path + "/") or name.startswith(module_path + ".")


def filter_decomp(c_source: str, module_path: str) -> tuple[str, int, int]:
    """Keep only functions whose name belongs to module_path or 'main.'.

    Returns (filtered_source, kept_count, total_count).
    """
    parts = re.split(r"^// Function: (.+)$", c_source, flags=re.MULTILINE)
    # parts = [preamble, name1, code1, name2, code2, ...]

    kept = []
    total = 0
    for i in range(1, len(parts) - 1, 2):
        total += 1
        name = parts[i].strip()
        if is_user_func(name, module_path):
            kept.append(f"// Function: {name}\n{parts[i + 1]}")

    return "".join(kept), len(kept), total


def collect_entries(meta: dict, args) -> list[dict]:
    """Build a flat list of binaries that have decomps to filter."""
    entries = []
    for repo_name, info in meta["repos"].items():
        if args.repo and repo_name != args.repo:
            continue
        if not info.get("cloned") or not info.get("compiled_at"):
            continue

        sname = safe_name(repo_name)
        for variant, bin_list in info.get("binaries", {}).items():
            if args.variant and variant != args.variant:
                continue
            for bin_name in bin_list:
                binary_path = BINARIES_DIR / sname / variant / bin_name
                decomp_path = DECOMPS_DIR / sname / variant / f"{bin_name}.c"
                output_path = FILTERED_DECOMPS_DIR / sname / variant / f"{bin_name}.c"

                if not binary_path.exists() or not decomp_path.exists():
                    continue

                entries.append({
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "binary_path": binary_path,
                    "decomp_path": decomp_path,
                    "output_path": output_path,
                })
    return entries


def main():
    parser = argparse.ArgumentParser(
        description="Filter Ghidra decomps to keep only user-implemented functions.",
    )
    parser.add_argument("--repo", type=str, default=None,
                        help="Filter to a specific repo (e.g. ollama/ollama)")
    parser.add_argument("--variant", type=str, default=None,
                        help="Filter to a specific variant (default, debug, stripped)")
    parser.add_argument("--force", action="store_true",
                        help="Re-filter even if output already exists")
    parser.add_argument("--max-repos", type=int, default=None,
                        help="Limit number of repos to process")
    args = parser.parse_args()

    meta = load_metadata()
    entries = collect_entries(meta, args)

    if not args.force:
        entries = [
            e for e in entries
            if not (e["output_path"].exists() and e["output_path"].stat().st_size > 0)
        ]

    if args.max_repos:
        seen: dict[str, None] = {}
        filtered = []
        for e in entries:
            if e["repo"] not in seen:
                if len(seen) >= args.max_repos:
                    continue
                seen[e["repo"]] = None
            filtered.append(e)
        entries = filtered

    if not entries:
        print("Nothing to filter (all outputs exist or no decomps found).")
        return

    print(f"Filtering {len(entries)} decomps...")

    # Cache module paths per binary (same binary_path → same module)
    mod_cache: dict[str, str | None] = {}
    succeeded = 0
    failed = 0
    total_kept = 0
    total_funcs = 0

    for entry in tqdm(entries, desc="Filtering", unit="bin"):
        bp = str(entry["binary_path"])
        if bp not in mod_cache:
            mod_cache[bp] = get_module_path(entry["binary_path"])

        mod_path = mod_cache[bp]
        if mod_path is None:
            tqdm.write(f"  SKIP {entry['repo']} {entry['variant']}/{entry['binary']} "
                       "(no module path)")
            failed += 1
            continue

        c_source = entry["decomp_path"].read_text()
        filtered, kept, total = filter_decomp(c_source, mod_path)

        if kept == 0:
            tqdm.write(f"  SKIP {entry['repo']} {entry['variant']}/{entry['binary']} "
                       f"(0/{total} functions matched {mod_path})")
            failed += 1
            continue

        entry["output_path"].parent.mkdir(parents=True, exist_ok=True)
        entry["output_path"].write_text(filtered)

        total_kept += kept
        total_funcs += total
        succeeded += 1
        tqdm.write(f"  {entry['repo']} {entry['variant']}/{entry['binary']}  "
                   f"{kept}/{total} funcs kept ({mod_path})")

    print(f"\n{'='*60}")
    print(f"  Succeeded:  {succeeded}")
    print(f"  Failed:     {failed}")
    print(f"  Functions:  {total_kept:,} kept / {total_funcs:,} total")
    print(f"  Output dir: {FILTERED_DECOMPS_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
