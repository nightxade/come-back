"""Validate filtered decomps against source_map.json ground truth.

Default mode (package-level):
  For each binary, classifies all `go tool nm` text symbols as user/non-user
  using both the filter's module-path logic and the source_map's repo-package
  ground truth.  Reports false positives and false negatives.

Deep mode (--deep, function-level):
  Additionally parses Go source files from data/repos/ to extract every
  func/method declaration, constructs expected symbol names, and verifies
  that every source-declared function present in the binary also appears
  in the filtered decomp.

Exits with code 1 if any mismatches are found.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from proj261.util import (
    BINARIES_DIR,
    DATA_DIR,
    DECOMPS_DIR,
    FILTERED_DECOMPS_DIR,
    METADATA_PATH,
    REPOS_DIR,
    safe_name,
)
from tqdm import tqdm

SOURCE_MAP_PATH = DATA_DIR / "source_map.json"


def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


def load_source_map() -> dict:
    return json.loads(SOURCE_MAP_PATH.read_text())


def get_module_path(binary_path: Path) -> str | None:
    try:
        r = subprocess.run(
            ["go", "version", "-m", str(binary_path)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "mod":
            return parts[1]
    return None


def get_nm_text_symbols(binary_path: Path) -> list[str]:
    """Get all T (text/code) symbols from a Go binary."""
    try:
        r = subprocess.run(
            ["go", "tool", "nm", str(binary_path)],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    syms = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 3 and parts[1] == "T":
            syms.append(parts[2])
    return syms


def is_user_func(name: str, module_path: str) -> bool:
    """Same logic as filter_decomps.is_user_func."""
    if name.startswith("main."):
        return True
    return name.startswith(module_path + "/") or name.startswith(module_path + ".")


def repo_packages_from_source_map(
    source_map: dict, repo: str, binary: str, variant: str, module_path: str,
) -> set[str] | None:
    """Derive the set of Go package paths that are repo-local from source_map."""
    for m in source_map["mappings"]:
        if m["repo"] == repo and m["binary"] == binary and m["variant"] == variant:
            pkgs = set()
            for f in m["source_files"]["repo"]:
                pkg_suffix = str(Path(f).parent)
                if pkg_suffix == ".":
                    pkgs.add(module_path)
                else:
                    pkgs.add(f"{module_path}/{pkg_suffix}")
            return pkgs
    return None


def is_repo_symbol(sym: str, repo_packages: set[str]) -> bool:
    """Check if a symbol belongs to a repo package (source_map ground truth)."""
    if sym.startswith("main."):
        return True
    for pkg in repo_packages:
        if sym.startswith(pkg + ".") or sym.startswith(pkg + "/"):
            return True
    return False


def collect_entries(meta: dict, source_map: dict, args) -> list[dict]:
    """Collect binaries that have both a filtered decomp and a source_map entry."""
    sm_index = {}
    for m in source_map["mappings"]:
        sm_index[(m["repo"], m["binary"], m["variant"])] = m

    entries = []
    for repo_name, info in meta["repos"].items():
        if args.repo and repo_name not in args.repo:
            continue
        if not info.get("cloned") or not info.get("compiled_at"):
            continue

        sname = safe_name(repo_name)
        for variant, bin_list in info.get("binaries", {}).items():
            if args.variant and variant != args.variant:
                continue
            for bin_name in bin_list:
                binary_path = BINARIES_DIR / sname / variant / bin_name
                filtered_path = FILTERED_DECOMPS_DIR / sname / variant / f"{bin_name}.c"

                if not binary_path.exists() or not filtered_path.exists():
                    continue
                if (repo_name, bin_name, variant) not in sm_index:
                    continue

                entries.append({
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "binary_path": binary_path,
                    "filtered_path": filtered_path,
                })
    return entries


# --------------------------------------------------------------------------- #
#  Package-level validation
# --------------------------------------------------------------------------- #

def validate_packages(entries, source_map):
    """Compare filter classification against source_map at the symbol level."""
    total_fp = 0
    total_fn = 0
    passed = 0
    failed = 0

    for entry in tqdm(entries, desc="Package-level", unit="bin"):
        mod = get_module_path(entry["binary_path"])
        if mod is None:
            tqdm.write(f"  SKIP {entry['repo']} {entry['variant']}/{entry['binary']} "
                       "(no module path)")
            continue

        repo_pkgs = repo_packages_from_source_map(
            source_map, entry["repo"], entry["binary"], entry["variant"], mod,
        )
        if repo_pkgs is None:
            tqdm.write(f"  SKIP {entry['repo']} {entry['variant']}/{entry['binary']} "
                       "(no source_map entry)")
            continue

        all_syms = get_nm_text_symbols(entry["binary_path"])
        go_syms = [s for s in all_syms if "." in s]

        filter_user = set()
        sm_user = set()
        for s in go_syms:
            if is_user_func(s, mod):
                filter_user.add(s)
            if is_repo_symbol(s, repo_pkgs):
                sm_user.add(s)

        false_neg = sm_user - filter_user
        false_pos = filter_user - sm_user
        total_fp += len(false_pos)
        total_fn += len(false_neg)

        if false_neg or false_pos:
            failed += 1
            tqdm.write(f"  FAIL {entry['repo']} {entry['variant']}/{entry['binary']}  "
                       f"FP={len(false_pos)} FN={len(false_neg)}")
            for f in sorted(false_neg)[:3]:
                tqdm.write(f"       MISSED: {f[:120]}")
            for f in sorted(false_pos)[:3]:
                tqdm.write(f"       EXTRA:  {f[:120]}")
        else:
            passed += 1
            tqdm.write(f"  OK   {entry['repo']} {entry['variant']}/{entry['binary']}  "
                       f"filter={len(filter_user)} source_map={len(sm_user)}")

    return passed, failed, total_fp, total_fn


# --------------------------------------------------------------------------- #
#  Function-level (deep) validation
# --------------------------------------------------------------------------- #

_FUNC_RE = re.compile(
    r"^func\s+(\w+)\s*[\[\(]",
    re.MULTILINE,
)
_METHOD_RE = re.compile(
    r"^func\s+\(\s*\w+\s+(\*?)(\w+)\s*\)\s+(\w+)\s*[\[\(]",
    re.MULTILINE,
)


def parse_source_declarations(file_path: Path, pkg_path: str) -> set[str]:
    """Parse a Go source file and return expected symbol names.

    Handles:
      func Foo(...)            -> pkg.Foo
      func (r *Bar) Baz(...)   -> pkg.(*Bar).Baz
      func (r Bar) Baz(...)    -> pkg.Bar.Baz
    """
    try:
        src = file_path.read_text(errors="replace")
    except Exception:
        return set()

    syms = set()

    for match in _FUNC_RE.finditer(src):
        name = match.group(1)
        syms.add(f"{pkg_path}.{name}")

    for match in _METHOD_RE.finditer(src):
        ptr, recv, name = match.group(1), match.group(2), match.group(3)
        if ptr:
            syms.add(f"{pkg_path}.(*{recv}).{name}")
        else:
            syms.add(f"{pkg_path}.{recv}.{name}")

    return syms


def validate_functions(entries, source_map):
    """For each source-declared function in the binary, verify it's in the filtered decomp.

    Distinguishes between:
      - filter_dropped: in raw decomp but not in filtered (filter bug)
      - ghidra_missed:  in binary (nm) but not in raw decomp (Ghidra limitation)
    """
    passed = 0
    failed = 0
    total_in_binary = 0
    total_in_decomp = 0
    total_dce = 0
    total_ghidra_missed = 0
    total_filter_dropped = 0

    for entry in tqdm(entries, desc="Function-level", unit="bin"):
        mod = get_module_path(entry["binary_path"])
        if mod is None:
            continue

        sname = safe_name(entry["repo"])
        repo_dir = REPOS_DIR / sname

        # Get repo source files for this binary from source_map
        sm_entry = None
        for m in source_map["mappings"]:
            if (m["repo"] == entry["repo"] and m["binary"] == entry["binary"]
                    and m["variant"] == entry["variant"]):
                sm_entry = m
                break
        if sm_entry is None:
            continue

        # Parse all source declarations
        source_syms = set()
        for rel_path in sm_entry["source_files"]["repo"]:
            file_path = repo_dir / rel_path
            pkg_suffix = str(Path(rel_path).parent)
            if pkg_suffix == ".":
                pkg_path = mod
            else:
                pkg_path = f"{mod}/{pkg_suffix}"
            source_syms |= parse_source_declarations(file_path, pkg_path)

        # Also add main.main / main.init for the main package
        main_dir = sm_entry["main_package"].removeprefix(mod).strip("/")
        main_file = repo_dir / main_dir / "main.go" if main_dir else repo_dir / "main.go"
        if main_file.exists():
            for sym in parse_source_declarations(main_file, mod + "/" + main_dir if main_dir else mod):
                # Remap to main.* since that's how they appear in the binary
                func_name = sym.rsplit(".", 1)[-1]
                source_syms.add(f"main.{func_name}")

        # Get nm symbols (what's actually in the binary)
        nm_syms = set(get_nm_text_symbols(entry["binary_path"]))

        # Get decomp function names from both raw and filtered
        filt_text = entry["filtered_path"].read_text()
        filtered_funcs = set(re.findall(r"^// Function: (.+)$", filt_text, re.MULTILINE))

        raw_path = DECOMPS_DIR / sname / entry["variant"] / f"{entry['binary']}.c"
        if raw_path.exists():
            raw_text = raw_path.read_text()
            raw_funcs = set(re.findall(r"^// Function: (.+)$", raw_text, re.MULTILINE))
        else:
            raw_funcs = set()

        def sym_in_set(sym, func_set):
            return sym in func_set or any(d.startswith(sym) for d in func_set)

        # For each source declaration: check binary, raw decomp, filtered decomp
        in_binary = 0
        in_decomp = 0
        dce = 0
        ghidra_missed = []
        filter_dropped = []

        for sym in source_syms:
            in_nm = sym in nm_syms or any(s.startswith(sym) for s in nm_syms)
            if not in_nm:
                dce += 1
                continue

            in_binary += 1

            in_filt = sym_in_set(sym, filtered_funcs)
            if in_filt:
                in_decomp += 1
                continue

            # Not in filtered decomp — why?
            in_raw = sym_in_set(sym, raw_funcs)
            if in_raw:
                filter_dropped.append(sym)
            else:
                ghidra_missed.append(sym)

        total_in_binary += in_binary
        total_in_decomp += in_decomp
        total_dce += dce
        total_ghidra_missed += len(ghidra_missed)
        total_filter_dropped += len(filter_dropped)

        has_filter_bug = len(filter_dropped) > 0

        if has_filter_bug:
            failed += 1
            tqdm.write(
                f"  FAIL {entry['repo']} {entry['variant']}/{entry['binary']}  "
                f"src={len(source_syms)} dce={dce} in_binary={in_binary} "
                f"in_decomp={in_decomp} "
                f"filter_dropped={len(filter_dropped)} ghidra_missed={len(ghidra_missed)}"
            )
            for m in sorted(filter_dropped)[:5]:
                tqdm.write(f"       FILTER BUG: {m}")
        else:
            passed += 1
            ghidra_note = f" ghidra_missed={len(ghidra_missed)}" if ghidra_missed else ""
            tqdm.write(
                f"  OK   {entry['repo']} {entry['variant']}/{entry['binary']}  "
                f"src={len(source_syms)} dce={dce} "
                f"in_binary={in_binary} in_decomp={in_decomp}"
                f"{ghidra_note}"
            )

    return passed, failed, total_in_binary, total_in_decomp, total_dce, total_ghidra_missed, total_filter_dropped


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Validate filtered decomps against source_map.json ground truth.",
    )
    parser.add_argument("--repo", type=str, nargs="*", default=None,
                        help="Validate specific repo(s) only")
    parser.add_argument("--variant", type=str, default=None,
                        help="Validate a specific variant only")
    parser.add_argument("--deep", action="store_true",
                        help="Also validate at the function level using source declarations")
    args = parser.parse_args()

    if not SOURCE_MAP_PATH.exists():
        print(f"Error: {SOURCE_MAP_PATH} not found. Run map-sources first.")
        sys.exit(1)

    meta = load_metadata()
    source_map = load_source_map()
    entries = collect_entries(meta, source_map, args)

    if not entries:
        print("Nothing to validate (no filtered decomps with source_map entries).")
        return

    any_failed = False

    # Package-level validation
    print(f"Validating {len(entries)} filtered decomps...\n")
    pkg_passed, pkg_failed, total_fp, total_fn = validate_packages(entries, source_map)

    print(f"\n{'='*60}")
    print(f"  Package-level validation")
    print(f"  Passed: {pkg_passed}")
    print(f"  Failed: {pkg_failed}")
    print(f"  Total false positives: {total_fp}")
    print(f"  Total false negatives: {total_fn}")
    print(f"{'='*60}")

    if pkg_failed:
        any_failed = True

    # Function-level (deep) validation
    if args.deep:
        print()
        fn_passed, fn_failed, in_bin, in_dec, dce, ghidra_missed, filter_dropped = \
            validate_functions(entries, source_map)

        print(f"\n{'='*60}")
        print(f"  Function-level validation")
        print(f"  Passed: {fn_passed}")
        print(f"  Failed: {fn_failed}")
        print(f"  Source declarations in binary: {in_bin}")
        print(f"  Found in filtered decomp:     {in_dec}")
        print(f"  Dead-code eliminated:         {dce}")
        print(f"  Ghidra missed (not a filter bug): {ghidra_missed}")
        print(f"  Filter dropped (BUG):             {filter_dropped}")
        print(f"{'='*60}")

        if fn_failed:
            any_failed = True

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
