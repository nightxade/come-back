"""Split Go source files into per-function files grouped by Go package.

Reads ``source_map.json`` to find repo source files for each binary, parses
each ``.go`` file to extract top-level function/method declarations with
bodies, then writes one ``.go`` file per function to
``data/source_chunked/{repo}/{variant}/{binary}/``.

Uses the **same naming pipeline** as ``chunk_decomps.py`` so that output
paths align between source chunks and decomp/inference chunks.
"""

import argparse
import json
import re
import shutil
from pathlib import Path

from proj261.data.chunk_decomps import (
    extract_func_part,
    extract_package,
    get_module_path,
    package_to_dir,
    sanitize_for_filename,
    simplify_generic_suffix,
)
from proj261.util import (
    BINARIES_DIR,
    CHUNKED_SOURCES_DIR,
    DATA_DIR,
    METADATA_PATH,
    REPOS_DIR,
    safe_name,
)
from tqdm import tqdm

SOURCE_MAP_PATH = DATA_DIR / "source_map.json"

# ---------------------------------------------------------------------------
#  Go function extraction (regex-based, no tree-sitter dependency)
# ---------------------------------------------------------------------------

# Matches the start of any top-level func declaration
_FUNC_START_RE = re.compile(r"^func\s", re.MULTILINE)

# func Name( ...  or  func Name[  (generic)
_PLAIN_FUNC_RE = re.compile(r"^func\s+(\w+)\s*[\[\(]")

# func (r *Type) Name( ...  or  func (r Type) Name[ ...
_METHOD_RE = re.compile(r"^func\s+\(\s*\w+\s+(\*?)(\w+)(?:\[.*?\])?\s*\)\s+(\w+)\s*[\[\(]")


def _find_func_body_end(source: str, brace_pos: int) -> int:
    """Find the position after the closing ``}`` that ends a function body.

    *brace_pos* is the index of the opening ``{``.  Returns the index one
    past the matching ``}``, or ``len(source)`` if unbalanced.

    Skips string literals (``"..."`` and backtick), rune literals, line
    comments (``//``), and block comments (``/* */``).
    """
    n = len(source)
    i = brace_pos + 1
    depth = 1

    while i < n and depth > 0:
        ch = source[i]

        # Line comment
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            i = source.find("\n", i)
            if i == -1:
                return n
            i += 1
            continue

        # Block comment
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            if end == -1:
                return n
            i = end + 2
            continue

        # Double-quoted string
        if ch == '"':
            i += 1
            while i < n and source[i] != '"':
                if source[i] == "\\":
                    i += 1  # skip escaped char
                i += 1
            i += 1  # skip closing "
            continue

        # Raw string (backtick)
        if ch == "`":
            i += 1
            while i < n and source[i] != "`":
                i += 1
            i += 1  # skip closing `
            continue

        # Rune literal
        if ch == "'":
            i += 1
            while i < n and source[i] != "'":
                if source[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1

        i += 1

    return i


def _find_opening_brace(source: str, start: int) -> int:
    """Find the opening ``{`` of a function body starting from *start*.

    Skips over the parameter list and return types.  Returns the index
    of ``{``, or -1 if not found before a blank-line gap (which would
    indicate this isn't a real function definition).
    """
    i = start
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "{":
            return i
        # If we hit two consecutive newlines before finding '{', bail out
        if ch == "\n" and i + 1 < n and source[i + 1] == "\n":
            return -1
        i += 1
    return -1


def extract_functions(source: str, pkg_path: str) -> list[tuple[str, str]]:
    """Extract top-level function declarations from Go source.

    Returns list of ``(fully_qualified_name, function_source)`` tuples.
    """
    functions: list[tuple[str, str]] = []

    for m in _FUNC_START_RE.finditer(source):
        line_start = m.start()
        # Get the full line(s) starting at 'func'
        line_end = source.find("\n", line_start)
        if line_end == -1:
            line_end = len(source)
        line = source[line_start:line_end]

        # Try method match first (more specific)
        mm = _METHOD_RE.match(line)
        if mm:
            ptr, recv, name = mm.group(1), mm.group(2), mm.group(3)
            if ptr:
                fq_name = f"{pkg_path}.(*{recv}).{name}"
            else:
                fq_name = f"{pkg_path}.{recv}.{name}"
        else:
            pm = _PLAIN_FUNC_RE.match(line)
            if pm:
                name = pm.group(1)
                fq_name = f"{pkg_path}.{name}"
            else:
                continue

        # Find opening brace
        brace_pos = _find_opening_brace(source, line_start)
        if brace_pos == -1:
            continue

        # Find closing brace
        body_end = _find_func_body_end(source, brace_pos)

        # Extract the full function text
        func_text = source[line_start:body_end]
        functions.append((fq_name, func_text))

    return functions


# ---------------------------------------------------------------------------
#  Chunking logic
# ---------------------------------------------------------------------------


def chunk_binary_sources(
    repo_dir: Path,
    source_files: list[str],
    main_package: str,
    module_path: str,
    output_dir: Path,
) -> dict | None:
    """Parse source files and write per-function .go files.

    Returns manifest dict on success, None if no functions found.
    """
    all_functions: list[tuple[str, str]] = []

    for rel_path in source_files:
        # Skip test files
        if rel_path.endswith("_test.go"):
            continue

        file_path = repo_dir / rel_path
        if not file_path.exists():
            continue

        try:
            src = file_path.read_text(errors="replace")
        except Exception:
            continue

        # Determine the package path for this file
        pkg_suffix = str(Path(rel_path).parent)
        if pkg_suffix == ".":
            # File in the repo root — could be the main package or the
            # module root package
            if main_package == module_path:
                # main package IS the module root
                pkg_path = "main"
            else:
                pkg_path = module_path
        else:
            # Check if this file's directory matches the main package
            main_suffix = main_package.removeprefix(module_path).strip("/")
            if main_suffix and pkg_suffix == main_suffix:
                pkg_path = "main"
            else:
                pkg_path = f"{module_path}/{pkg_suffix}"

        funcs = extract_functions(src, pkg_path)
        all_functions.extend(funcs)

    if not all_functions:
        return None

    # Clean out previous output
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    packages: dict[str, int] = {}
    manifest_functions = []
    seen_filenames: dict[str, int] = {}

    for func_name, func_text in all_functions:
        pkg = extract_package(func_name, module_path)
        func_part = extract_func_part(func_name, pkg)

        pkg_dir_name = package_to_dir(pkg, module_path)
        func_file_name = sanitize_for_filename(simplify_generic_suffix(func_part))

        # Handle duplicate filenames within the same package dir
        key = f"{pkg_dir_name}/{func_file_name}"
        if key in seen_filenames:
            seen_filenames[key] += 1
            func_file_name = f"{func_file_name}_{seen_filenames[key]}"
        else:
            seen_filenames[key] = 0

        pkg_subdir = output_dir / pkg_dir_name
        pkg_subdir.mkdir(parents=True, exist_ok=True)

        func_file = pkg_subdir / f"{func_file_name}.go"
        func_file.write_text(func_text)

        packages[pkg] = packages.get(pkg, 0) + 1
        manifest_functions.append({
            "function": func_name,
            "package": pkg,
            "file": f"{pkg_dir_name}/{func_file_name}.go",
        })

    manifest = {
        "total_functions": len(all_functions),
        "total_packages": len(packages),
        "functions": manifest_functions,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# ---------------------------------------------------------------------------
#  Entry collection & CLI
# ---------------------------------------------------------------------------


def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


def load_source_map() -> dict:
    return json.loads(SOURCE_MAP_PATH.read_text())


def collect_entries(meta: dict, source_map: dict, args) -> list[dict]:
    """Build a flat list of binaries that have source map entries to chunk."""
    # Index source_map for fast lookup
    sm_index: dict[tuple[str, str, str], dict] = {}
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
                sm_entry = sm_index.get((repo_name, bin_name, variant))
                if sm_entry is None:
                    continue

                binary_path = BINARIES_DIR / sname / variant / bin_name
                if not binary_path.exists():
                    continue

                output_dir = CHUNKED_SOURCES_DIR / sname / variant / bin_name

                entries.append({
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "binary_path": binary_path,
                    "output_dir": output_dir,
                    "source_files": sm_entry["source_files"]["repo"],
                    "main_package": sm_entry["main_package"],
                })
    return entries


def main():
    parser = argparse.ArgumentParser(
        description="Split Go source files into per-function files by Go package.",
    )
    parser.add_argument("--repo", type=str, nargs="*", default=None,
                        help="Filter to specific repo(s) (e.g. ollama/ollama)")
    parser.add_argument("--variant", type=str, default=None,
                        help="Filter to a specific variant (default, debug, stripped)")
    parser.add_argument("--max-repos", type=int, default=None,
                        help="Limit number of repos to process")
    parser.add_argument("--force", action="store_true",
                        help="Re-chunk even if output already exists")
    args = parser.parse_args()

    if not SOURCE_MAP_PATH.exists():
        print(f"Error: {SOURCE_MAP_PATH} not found. Run map-sources first.")
        return

    meta = load_metadata()
    source_map = load_source_map()
    entries = collect_entries(meta, source_map, args)

    if not args.force:
        entries = [
            e for e in entries
            if not (e["output_dir"] / "manifest.json").exists()
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
        print("Nothing to chunk (all outputs exist or no source map entries found).")
        return

    print(f"Chunking sources for {len(entries)} binaries...")

    # Cache module paths per binary
    mod_cache: dict[str, str | None] = {}
    succeeded = 0
    failed = 0
    total_funcs = 0

    for entry in tqdm(entries, desc="Chunking sources", unit="bin"):
        bp = str(entry["binary_path"])
        if bp not in mod_cache:
            mod_cache[bp] = get_module_path(entry["binary_path"])

        mod_path = mod_cache[bp]
        if mod_path is None:
            tqdm.write(f"  SKIP {entry['repo']} {entry['variant']}/{entry['binary']} "
                       "(no module path)")
            failed += 1
            continue

        sname = safe_name(entry["repo"])
        repo_dir = REPOS_DIR / sname

        manifest = chunk_binary_sources(
            repo_dir=repo_dir,
            source_files=entry["source_files"],
            main_package=entry["main_package"],
            module_path=mod_path,
            output_dir=entry["output_dir"],
        )
        if manifest is None:
            tqdm.write(f"  SKIP {entry['repo']} {entry['variant']}/{entry['binary']} "
                       "(no functions found)")
            failed += 1
            continue

        succeeded += 1
        total_funcs += manifest["total_functions"]
        tqdm.write(f"  {entry['repo']} {entry['variant']}/{entry['binary']}  "
                   f"{manifest['total_functions']} funcs ({manifest['total_packages']} pkgs)")

    print(f"\n{'='*60}")
    print(f"  Succeeded:  {succeeded}")
    print(f"  Failed:     {failed}")
    print(f"  Functions:  {total_funcs:,} total")
    print(f"  Output dir: {CHUNKED_SOURCES_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
