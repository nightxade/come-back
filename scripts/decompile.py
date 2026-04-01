#!/usr/bin/env python3
"""Decompile compiled Go binaries via Ghidra headless (PyGhidra).

Reads metadata.json to discover successfully compiled binaries, then uses
PyGhidra's modern API (open_project / program_context / analyze) to run
Ghidra's decompiler on each one.  Produces one .c file per binary under
data/decomps/<owner__repo>/<variant>/<binary>.c
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from tqdm import tqdm

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
METADATA_PATH = DATA_DIR / "metadata.json"
BINARIES_DIR = DATA_DIR / "binaries"
DECOMPS_DIR = DATA_DIR / "decomps"

GHIDRA_INSTALL = Path(os.environ.get("GHIDRA_INSTALL_DIR", "/opt/ghidra"))

FUNC_DECOMPILE_TIMEOUT = 30   # seconds per function
ANALYSIS_TIMEOUT = 300         # seconds per binary


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def safe_name(full_name: str) -> str:
    return full_name.replace("/", "__")


def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


# --------------------------------------------------------------------------- #
#  Core
# --------------------------------------------------------------------------- #

def decompile_program(program, output_path: Path) -> bool:
    """Decompile all functions in an already-analyzed program.

    Uses DecompInterface directly — no script runner overhead.
    Returns True on success.
    """
    import pyghidra
    from ghidra.app.decompiler import DecompInterface

    output_path.parent.mkdir(parents=True, exist_ok=True)

    decompiler = DecompInterface()
    decompiler.openProgram(program)
    monitor = pyghidra.task_monitor()

    try:
        functions = list(program.getFunctionManager().getFunctions(True))
        with open(output_path, "w") as f:
            for func in functions:
                result = decompiler.decompileFunction(
                    func, FUNC_DECOMPILE_TIMEOUT, monitor,
                )
                decomp = result.getDecompiledFunction()
                if decomp is not None:
                    c_code = decomp.getC()
                    if c_code:
                        f.write(f"// Function: {func.getName()}\n")
                        f.write(str(c_code))
                        f.write("\n")
    finally:
        decompiler.dispose()

    return output_path.exists() and output_path.stat().st_size > 0


def process_binary(project, binary_path: Path, output_path: Path) -> bool:
    """Import, analyze, and decompile a single binary within an open project.

    The program is loaded into the project, analyzed, decompiled, then the
    project file is deleted to keep disk use low.
    """
    import pyghidra

    program_name = binary_path.name
    monitor = pyghidra.task_monitor(timeout=ANALYSIS_TIMEOUT)

    try:
        # Load the binary into the project
        loader = pyghidra.program_loader().project(project)
        loader = loader.source(str(binary_path)).name(program_name)

        with loader.load() as load_results:
            load_results.save(monitor)

        # Open, analyze, decompile
        with pyghidra.program_context(project, f"/{program_name}") as program:
            pyghidra.analyze(program, monitor)
            ok = decompile_program(program, output_path)

        # Remove program from project to free disk / memory
        domain_file = project.getProjectData().getFile(f"/{program_name}")
        if domain_file is not None:
            domain_file.delete()

        return ok

    except Exception as e:
        tqdm.write(f"    ERROR: {e}")
        return False


def collect_binaries(meta: dict, repo_filter: str | None) -> list[dict]:
    """Build a flat list of binaries to decompile from metadata."""
    entries = []
    for repo_name, info in meta["repos"].items():
        if repo_filter and repo_name != repo_filter:
            continue
        if not info.get("cloned") or not info.get("compiled_at"):
            continue

        sname = safe_name(repo_name)
        for variant, bin_list in info.get("binaries", {}).items():
            for bin_name in bin_list:
                binary_path = BINARIES_DIR / sname / variant / bin_name
                output_path = DECOMPS_DIR / sname / variant / f"{bin_name}.c"
                entries.append({
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "binary_path": binary_path,
                    "output_path": output_path,
                })
    return entries


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Decompile compiled Go binaries using Ghidra headless.",
    )
    parser.add_argument("--repo", type=str, default=None,
                        help="Decompile binaries for a specific repo only (e.g. ollama/ollama)")
    parser.add_argument("--variant", type=str, default=None,
                        help="Only decompile a specific variant (default, debug, stripped)")
    parser.add_argument("--force", action="store_true",
                        help="Re-decompile even if output already exists")
    args = parser.parse_args()

    meta = load_metadata()
    entries = collect_binaries(meta, args.repo)

    if args.variant:
        entries = [e for e in entries if e["variant"] == args.variant]

    if not args.force:
        entries = [
            e for e in entries
            if not (e["output_path"].exists() and e["output_path"].stat().st_size > 0)
        ]

    if not entries:
        print("Nothing to decompile (all outputs exist or no binaries found).")
        return

    print(f"Decompiling {len(entries)} binaries...")

    import pyghidra
    pyghidra.start(install_dir=GHIDRA_INSTALL)

    succeeded = 0
    failed = 0

    with tempfile.TemporaryDirectory(prefix="ghidra_proj_") as tmpdir:
        with pyghidra.open_project(tmpdir, "decomp", create=True) as project:
            for entry in tqdm(entries, desc="Decompiling", unit="bin"):
                binary_path = entry["binary_path"]
                output_path = entry["output_path"]

                if not binary_path.exists():
                    tqdm.write(f"  SKIP {binary_path} (binary missing)")
                    failed += 1
                    continue

                tqdm.write(f"  {entry['repo']}  {entry['variant']}/{entry['binary']}")
                ok = process_binary(project, binary_path, output_path)
                if ok:
                    succeeded += 1
                else:
                    failed += 1

    print(f"\n{'='*60}")
    print(f"  Succeeded:  {succeeded}")
    print(f"  Failed:     {failed}")
    print(f"  Output dir: {DECOMPS_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
