"""Split filtered decomps into token-budgeted chunks grouped by Go package.

Filtered decomps for large Go projects can still far exceed LLM context
windows.  This step groups functions by their Go package (extracted from
fully-qualified names in ``// Function:`` markers), then greedily packs
packages into chunks that fit within a configurable token budget.

Writes output to ``data/decomps_chunked/{repo}/{variant}/{binary}/``
with numbered chunk files and a ``manifest.json``.
"""

import argparse
import json
import re
from pathlib import Path

from proj261.util import (
    CHUNKED_DECOMPS_DIR,
    FILTERED_DECOMPS_DIR,
    METADATA_PATH,
    safe_name,
)
from tqdm import tqdm

DEFAULT_BUDGET = 1_000_000  # tokens


def estimate_tokens(text: str) -> int:
    """Estimate token count from character count.

    Ghidra C pseudocode tokenizes at ~2.4 chars/token (measured against
    the Gemini tokenizer), much denser than the generic ~4 chars/token
    rule of thumb for natural-language text.  We use /2 to leave headroom.
    """
    return len(text) // 2


def extract_package(func_name: str) -> str:
    """Extract Go package path from a fully-qualified function name.

    Examples:
        github.com/foo/bar.Func           -> github.com/foo/bar
        github.com/foo/bar.(*T).Method    -> github.com/foo/bar
        github.com/foo/bar.Type.Method    -> github.com/foo/bar
        github.com/foo/bar.init.func1     -> github.com/foo/bar
        main.main                         -> main
    """
    last_slash = func_name.rfind("/")
    if last_slash == -1:
        # No slash — e.g. "main.main" → package is "main"
        dot = func_name.find(".")
        if dot == -1:
            return func_name
        return func_name[:dot]
    # Find the first '.' after the last '/'
    dot = func_name.find(".", last_slash)
    if dot == -1:
        return func_name
    return func_name[:dot]


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


def group_by_package(
    functions: list[tuple[str, str]],
) -> dict[str, list[tuple[str, str]]]:
    """Group functions by their Go package."""
    packages: dict[str, list[tuple[str, str]]] = {}
    for name, block in functions:
        pkg = extract_package(name)
        packages.setdefault(pkg, []).append((name, block))
    return packages


def pack_chunks(
    packages: dict[str, list[tuple[str, str]]],
    budget: int,
) -> list[list[tuple[str, str]]]:
    """Greedily pack packages into chunks under the token budget.

    Packages are sorted largest-first.  If a single package exceeds the
    budget its functions are sub-chunked sequentially.
    """
    # Compute total text and token estimate per package
    pkg_sizes: list[tuple[str, int, str]] = []
    for pkg, funcs in packages.items():
        text = "".join(block for _, block in funcs)
        pkg_sizes.append((pkg, estimate_tokens(text), text))

    # Sort by size descending (largest first)
    pkg_sizes.sort(key=lambda x: x[1], reverse=True)

    chunks: list[list[tuple[str, str]]] = []
    current_chunk: list[tuple[str, str]] = []
    current_tokens = 0

    for pkg, pkg_tokens, _ in pkg_sizes:
        funcs = packages[pkg]

        if pkg_tokens <= budget:
            # Package fits in a single chunk — try to add to current
            if current_tokens + pkg_tokens <= budget:
                current_chunk.extend(funcs)
                current_tokens += pkg_tokens
            else:
                # Start a new chunk
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = list(funcs)
                current_tokens = pkg_tokens
        else:
            # Oversized package — flush current chunk, then sub-chunk
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_tokens = 0

            sub_chunk: list[tuple[str, str]] = []
            sub_tokens = 0
            for name, block in funcs:
                ftokens = estimate_tokens(block)
                if sub_tokens + ftokens > budget and sub_chunk:
                    chunks.append(sub_chunk)
                    sub_chunk = []
                    sub_tokens = 0
                sub_chunk.append((name, block))
                sub_tokens += ftokens

            if sub_chunk:
                # Try to merge remainder into a new current_chunk
                current_chunk = sub_chunk
                current_tokens = sub_tokens

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def chunk_binary(
    decomp_path: Path,
    output_dir: Path,
    budget: int,
) -> dict | None:
    """Chunk a single filtered decomp file.

    Returns manifest dict on success, None on failure.
    """
    c_source = decomp_path.read_text()
    functions = split_functions(c_source)
    if not functions:
        return None

    packages = group_by_package(functions)
    chunks = pack_chunks(packages, budget)

    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_chunks = []
    for idx, chunk_funcs in enumerate(chunks):
        chunk_text = "".join(block for _, block in chunk_funcs)
        chunk_file = output_dir / f"chunk_{idx:03d}.c"
        chunk_file.write_text(chunk_text)
        manifest_chunks.append({
            "file": chunk_file.name,
            "functions": len(chunk_funcs),
            "estimated_tokens": estimate_tokens(chunk_text),
        })

    manifest = {
        "total_functions": len(functions),
        "total_packages": len(packages),
        "total_chunks": len(chunks),
        "budget": budget,
        "chunks": manifest_chunks,
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
                filtered_path = FILTERED_DECOMPS_DIR / sname / variant / f"{bin_name}.c"
                output_dir = CHUNKED_DECOMPS_DIR / sname / variant / bin_name

                if not filtered_path.exists():
                    continue

                entries.append({
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "filtered_path": filtered_path,
                    "output_dir": output_dir,
                })
    return entries


def main():
    parser = argparse.ArgumentParser(
        description="Split filtered decomps into token-budgeted chunks by Go package.",
    )
    parser.add_argument("--repo", type=str, default=None,
                        help="Filter to a specific repo (e.g. ollama/ollama)")
    parser.add_argument("--variant", type=str, default=None,
                        help="Filter to a specific variant (default, debug, stripped)")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help=f"Target token budget per chunk (default: {DEFAULT_BUDGET:,})")
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

    print(f"Chunking {len(entries)} decomps (budget={args.budget:,} tokens)...")

    succeeded = 0
    failed = 0
    total_chunks = 0
    total_funcs = 0

    for entry in tqdm(entries, desc="Chunking", unit="bin"):
        manifest = chunk_binary(
            entry["filtered_path"],
            entry["output_dir"],
            args.budget,
        )
        if manifest is None:
            tqdm.write(f"  SKIP {entry['repo']} {entry['variant']}/{entry['binary']} "
                       "(no functions found)")
            failed += 1
            continue

        succeeded += 1
        total_chunks += manifest["total_chunks"]
        total_funcs += manifest["total_functions"]
        tqdm.write(f"  {entry['repo']} {entry['variant']}/{entry['binary']}  "
                   f"{manifest['total_functions']} funcs -> {manifest['total_chunks']} chunks "
                   f"({manifest['total_packages']} pkgs)")

    print(f"\n{'='*60}")
    print(f"  Succeeded:  {succeeded}")
    print(f"  Failed:     {failed}")
    print(f"  Functions:  {total_funcs:,} total")
    print(f"  Chunks:     {total_chunks:,} total")
    print(f"  Output dir: {CHUNKED_DECOMPS_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
