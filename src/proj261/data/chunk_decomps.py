"""Split filtered decomps into per-function files grouped by Go package.

Each function from the filtered decomp gets its own ``.c`` file, organized
into package subdirectories under
``data/decomps_chunked/{repo}/{variant}/{binary}/``.

A ``manifest.json`` alongside the package directories lists every function
with its package, filename, and estimated token count.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from proj261.util import (
    BINARIES_DIR,
    CHUNKED_DECOMPS_DIR,
    FILTERED_DECOMPS_DIR,
    METADATA_PATH,
    safe_name,
)
from tqdm import tqdm


def estimate_tokens(text: str) -> int:
    """Estimate token count from character count.

    Ghidra C pseudocode tokenizes at ~2.4 chars/token (measured against
    the Gemini tokenizer), much denser than the generic ~4 chars/token
    rule of thumb for natural-language text.  We use /2 to leave headroom.
    """
    return len(text) // 2


def get_module_path(binary_path: Path) -> str | None:
    """Extract the Go module path from a compiled binary via ``go version -m``."""
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


def extract_package(func_name: str, module_path: str = "") -> str:
    """Extract Go package path from a fully-qualified function name.

    When *module_path* is provided, uses it to reliably locate the
    boundary between the package path and the function/method part.
    This is necessary because Go function names can embed additional
    ``/`` and ``.`` characters in:

    * generic type parameters — ``pkg.F[github.com/other.T]``
    * method receivers — ``pkg.(*T[github.com/other.U]).M``
    * embedded interface methods — ``pkg.Type.github.com/other.Method``

    A purely syntactic parse (find-last-slash, find-first-dot) is
    confused by all three cases.  With the module path we instead walk
    path segments after the module prefix and stop at the first segment
    containing a ``.``, which marks the start of the function part.

    Examples (module_path="github.com/foo/bar"):
        github.com/foo/bar.Func                          -> github.com/foo/bar
        github.com/foo/bar/sub.(*T).Method               -> github.com/foo/bar/sub
        github.com/foo/bar/sub.(*T[github.com/x.Y]).M    -> github.com/foo/bar/sub
        github.com/foo/bar/sub.Type.github.com/x.Method  -> github.com/foo/bar/sub
        github.com/foo/bar/sub.F[go.shape.struct_{...}]  -> github.com/foo/bar/sub
        main.main                                        -> main
    """
    if func_name.startswith("main."):
        return "main"

    if module_path:
        if func_name.startswith(module_path + "."):
            return module_path
        if func_name.startswith(module_path + "/"):
            # Walk sub-path segments; the first segment containing '.'
            # marks the boundary between package and function.
            rest = func_name[len(module_path) + 1:]
            segments = rest.split("/")
            pkg_segments: list[str] = []
            for seg in segments:
                dot = seg.find(".")
                if dot != -1:
                    pkg_segments.append(seg[:dot])
                    break
                pkg_segments.append(seg)
            return module_path + "/" + "/".join(pkg_segments)

    # Fallback when no module_path is available: truncate at the first
    # character that can't appear in a package path.
    cut = len(func_name)
    for ch in "(*[":
        idx = func_name.find(ch)
        if idx != -1 and idx < cut:
            cut = idx
    pkg_part = func_name[:cut].rstrip(".")

    last_slash = pkg_part.rfind("/")
    if last_slash == -1:
        dot = pkg_part.find(".")
        if dot == -1:
            return pkg_part
        return pkg_part[:dot]
    dot = pkg_part.find(".", last_slash)
    if dot == -1:
        return pkg_part
    return pkg_part[:dot]


def extract_func_part(func_name: str, package: str) -> str:
    """Extract the function/method part after the package prefix.

    Examples:
        github.com/foo/bar.Func,        pkg=github.com/foo/bar -> Func
        github.com/foo/bar.(*T).Method, pkg=github.com/foo/bar -> (*T).Method
        main.main,                      pkg=main               -> main
    """
    # The function part starts after "package."
    prefix = package + "."
    if func_name.startswith(prefix):
        return func_name[len(prefix):]
    return func_name


def _type_kind(content: str) -> str:
    """Extract a short type label from a generic type parameter.

    For ``go.shape.*`` descriptors returns the shape kind (struct,
    interface, …).  For concrete types returns the last ``.``-separated
    component (e.g. ``github.com/foo/bar.Baz`` → ``Baz``).
    """
    # go.shape monomorphization descriptors
    if content.startswith("go.shape."):
        shape = content[len("go.shape."):]
        for prefix in ("struct", "interface", "func", "chan"):
            if shape.startswith(prefix):
                return prefix
        if shape.startswith("[]"):
            return "slice"
        if shape.startswith("map["):
            return "map"
        if shape.startswith("*"):
            return "ptr"
        m = re.match(r"[a-zA-Z0-9_]+", shape)
        return m.group(0) if m else "generic"

    # Concrete type parameters — extract a readable short name
    if content.startswith("[]"):
        return "slice"
    if content.startswith("map["):
        return "map"
    # Strip leading pointer
    c = content.lstrip("*")
    # Take only the first type param (cut at first top-level comma)
    depth = 0
    for i, ch in enumerate(c):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            c = c[:i]
            break
    # Extract last component: github.com/foo/bar.Type → Type
    dot = c.rfind(".")
    if dot != -1:
        m = re.match(r"[a-zA-Z0-9_]+", c[dot + 1:])
        if m:
            return m.group(0)
    m = re.match(r"[a-zA-Z0-9_]+", c)
    return m.group(0) if m else "generic"


def simplify_generic_suffix(name: str) -> str:
    """Simplify Go generic type parameters in a function name to a short tag.

    Handles both ``go.shape.*`` monomorphization descriptors and concrete
    type instantiations (e.g. ``[github.com/foo.Bar]``).  The verbose
    type parameter is replaced with a short label derived from the type
    kind or name, while preserving any suffix after the closing ``]``
    (e.g. a ``.Method`` part).

    Examples::

        AsyncTask[go.shape.struct_{...}]        -> AsyncTask_struct
        (*Map[github.com/foo.Bar, cty.Value]).K -> (*Map_Bar).K
        (*Once[*github.com/foo.Input]).Resolve  -> (*Once_Input).Resolve
        NewPromise[go.shape.map[string]...]     -> NewPromise_map

    Multiple instantiations that simplify to the same tag will collide,
    but the duplicate-filename logic in ``chunk_binary`` handles that.
    """
    bracket = name.find("[")
    if bracket == -1:
        return name

    base = name[:bracket]
    if not base:
        return name

    # Find the matching closing bracket to preserve the suffix after it
    depth = 1
    i = bracket + 1
    while i < len(name) and depth > 0:
        if name[i] == "[":
            depth += 1
        elif name[i] == "]":
            depth -= 1
        i += 1
    suffix = name[i:] if depth == 0 else ""

    content = name[bracket + 1 : i - 1] if depth == 0 else name[bracket + 1 :]
    kind = _type_kind(content)

    return f"{base}_{kind}{suffix}"


MAX_FILENAME_LEN = 200  # leave room for path prefix and .c extension


def sanitize_for_filename(name: str) -> str:
    """Sanitize a string for use as a filename component.

    Long names (e.g. Go generic instantiations) are truncated with a
    short hash suffix for uniqueness.
    """
    name = name.replace("/", "__")
    name = re.sub(r"[*()\[\]{}<>,;:\"' ]", "", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    name = name or "unnamed"
    if len(name) > MAX_FILENAME_LEN:
        h = hashlib.sha256(name.encode()).hexdigest()[:12]
        name = name[:MAX_FILENAME_LEN - 13] + "_" + h
    return name


def package_to_dir(pkg: str, module_path: str) -> str:
    """Convert a Go package path to a relative directory path.

    Strips the module path prefix so the directory structure mirrors the
    Go source layout.

    Examples (module_path="github.com/hashicorp/terraform"):
        main                                              -> main
        github.com/hashicorp/terraform                    -> terraform
        github.com/hashicorp/terraform/internal/tfplugin5 -> internal/tfplugin5
    """
    if pkg == "main":
        return "main"
    if pkg == module_path:
        # Root module package — use last path component
        return pkg.rsplit("/", 1)[-1]
    if pkg.startswith(module_path + "/"):
        return pkg[len(module_path) + 1:]
    # Fallback for packages outside the module (shouldn't happen with
    # filtered decomps, but just in case)
    return sanitize_for_filename(pkg)


def split_functions(c_source: str) -> list[tuple[str, str]]:
    """Split a decomp on ``// Function:`` markers.

    Returns list of (function_name, full_block) tuples where full_block
    includes the ``// Function:`` header line.
    """
    parts = re.split(r"^// Function: (.+)$", c_source, flags=re.MULTILINE)
    functions = []
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].strip()
        code = parts[i + 1]
        functions.append((name, f"// Function: {name}\n{code}"))
    return functions


def chunk_binary(decomp_path: Path, output_dir: Path, module_path: str) -> dict | None:
    """Split a single filtered decomp into per-function files by package.

    Returns manifest dict on success, None on failure.
    """
    c_source = decomp_path.read_text()
    functions = split_functions(c_source)
    if not functions:
        return None

    # Clean out any previous chunking output
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    packages: dict[str, int] = {}
    manifest_functions = []
    seen_filenames: dict[str, int] = {}  # track duplicates per package dir

    for func_name, block in functions:
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

        func_file = pkg_subdir / f"{func_file_name}.c"
        func_file.write_text(block)

        packages[pkg] = packages.get(pkg, 0) + 1
        manifest_functions.append({
            "function": func_name,
            "package": pkg,
            "file": f"{pkg_dir_name}/{func_file_name}.c",
            "estimated_tokens": estimate_tokens(block),
        })

    manifest = {
        "total_functions": len(functions),
        "total_packages": len(packages),
        "functions": manifest_functions,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


def collect_entries(meta: dict, args) -> list[dict]:
    """Build a flat list of binaries that have filtered decomps to chunk."""
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
                filtered_path = FILTERED_DECOMPS_DIR / sname / variant / f"{bin_name}.c"
                output_dir = CHUNKED_DECOMPS_DIR / sname / variant / bin_name

                if not binary_path.exists() or not filtered_path.exists():
                    continue

                entries.append({
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "binary_path": binary_path,
                    "filtered_path": filtered_path,
                    "output_dir": output_dir,
                })
    return entries


def main():
    parser = argparse.ArgumentParser(
        description="Split filtered decomps into per-function files by Go package.",
    )
    parser.add_argument("--repo", type=str, default=None,
                        help="Filter to a specific repo (e.g. ollama/ollama)")
    parser.add_argument("--variant", type=str, default=None,
                        help="Filter to a specific variant (default, debug, stripped)")
    parser.add_argument("--max-repos", type=int, default=None,
                        help="Limit number of repos to process")
    parser.add_argument("--force", action="store_true",
                        help="Re-chunk even if output already exists")
    args = parser.parse_args()

    meta = load_metadata()
    entries = collect_entries(meta, args)

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
        print("Nothing to chunk (all outputs exist or no filtered decomps found).")
        return

    print(f"Chunking {len(entries)} decomps...")

    # Cache module paths per binary (same binary_path → same module)
    mod_cache: dict[str, str | None] = {}
    succeeded = 0
    failed = 0
    total_funcs = 0

    for entry in tqdm(entries, desc="Chunking", unit="bin"):
        bp = str(entry["binary_path"])
        if bp not in mod_cache:
            mod_cache[bp] = get_module_path(entry["binary_path"])

        mod_path = mod_cache[bp]
        if mod_path is None:
            tqdm.write(f"  SKIP {entry['repo']} {entry['variant']}/{entry['binary']} "
                       "(no module path)")
            failed += 1
            continue

        manifest = chunk_binary(entry["filtered_path"], entry["output_dir"], mod_path)
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
    print(f"  Output dir: {CHUNKED_DECOMPS_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
