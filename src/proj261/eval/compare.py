#!/usr/bin/env python3
"""Compare inference output against original source chunks.

Matches source chunks to inference output by file stem, calls a pluggable
comparison metric, and reports results as JSON + stdout summary.
"""

import argparse
import json
import sys
from pathlib import Path

from proj261.util import (
    CHUNKED_DECOMPS_DIR,
    CHUNKED_SOURCES_DIR,
    METADATA_PATH,
    OUT_DIR,
    PRED_DIR,
    safe_name,
)
from proj261.eval.comparisons import get_comparator, get_metric_module
from tqdm import tqdm


RESULTS_DIR = OUT_DIR / "results"


# --------------------------------------------------------------------------- #
#  Entry collection
# --------------------------------------------------------------------------- #

def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


def collect_entries(meta: dict, args) -> list[dict]:
    """Build a flat list of binaries that have both source and decomp chunks."""
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
                source_dir = CHUNKED_SOURCES_DIR / sname / variant / bin_name
                decomp_dir = CHUNKED_DECOMPS_DIR / sname / variant / bin_name

                # Both source chunks and decomp chunks must exist
                if not (source_dir / "manifest.json").exists():
                    continue
                if not (decomp_dir / "manifest.json").exists():
                    continue

                entries.append({
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "source_dir": str(source_dir),
                    "decomp_dir": str(decomp_dir),
                })

    # Limit repos
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

    # Limit total binaries
    if args.max_binaries:
        entries = entries[: args.max_binaries]

    return entries


# --------------------------------------------------------------------------- #
#  Per-binary evaluation
# --------------------------------------------------------------------------- #

def evaluate_binary(entry, compare_fn, aggregate_fn, metric, force):
    """Evaluate a single binary: match decomp chunks to source, run comparator.

    Returns the result dict for this binary.
    """
    repo = entry["repo"]
    variant = entry["variant"]
    binary = entry["binary"]
    sname = safe_name(repo)

    source_dir = Path(entry["source_dir"])
    decomp_dir = Path(entry["decomp_dir"])
    inference_dir = PRED_DIR / sname / variant / binary

    # Output path for eval results
    results_dir = RESULTS_DIR / metric / sname / variant
    results_path = results_dir / f"{binary}.json"

    # Skip if already evaluated — but invalidate the cache when inference
    # has been updated since the last comparison (i.e. new predictions were
    # added for functions that were previously missing).
    if not force and results_path.exists():
        try:
            existing = json.loads(results_path.read_text())
            stale = False
            if existing.get("skipped_no_inference", 0) > 0:
                inf_meta = inference_dir / "metadata.json"
                if inf_meta.exists() and inf_meta.stat().st_mtime > results_path.stat().st_mtime:
                    stale = True
            if not stale:
                return existing
        except json.JSONDecodeError:
            pass

    # Load manifests
    source_manifest = json.loads((source_dir / "manifest.json").read_text())
    decomp_manifest = json.loads((decomp_dir / "manifest.json").read_text())

    # Build lookup: file_stem -> source entry
    source_by_stem = {}
    for src_entry in source_manifest["functions"]:
        stem = src_entry["file"].removesuffix(".go")
        source_by_stem[stem] = src_entry

    # Iterate decomp manifest entries
    function_results = {}
    skipped_no_source = 0
    skipped_no_inference = 0

    for decomp_entry in decomp_manifest["functions"]:
        file_stem = decomp_entry["file"].removesuffix(".c")

        # Match to source
        src_entry = source_by_stem.get(file_stem)
        if src_entry is None:
            skipped_no_source += 1
            continue

        # Read source .go file
        source_file = source_dir / src_entry["file"]
        if not source_file.exists():
            skipped_no_source += 1
            continue
        source_code = source_file.read_text()

        # Read inference .go file
        inference_file = inference_dir / f"{file_stem}.go"
        if not inference_file.exists():
            skipped_no_inference += 1
            continue
        inferred_code = inference_file.read_text()

        # Read decomp .c file
        decomp_file = decomp_dir / decomp_entry["file"]
        decomp_code = decomp_file.read_text() if decomp_file.exists() else ""

        # Build metadata for comparator
        metadata = {
            "source_function": decomp_entry.get("source_function", ""),
            "functions": decomp_entry.get("functions", []),
            "package": decomp_entry.get("package", ""),
            "estimated_tokens": decomp_entry.get("estimated_tokens", 0),
        }

        # Run comparison
        scores = compare_fn(source_code, inferred_code, decomp_code, metadata)
        function_results[file_stem] = scores

    # Aggregate
    all_scores = list(function_results.values())
    aggregate = aggregate_fn(all_scores) if all_scores else aggregate_fn([])

    result = {
        "repo": repo,
        "variant": variant,
        "binary": binary,
        "metric": metric,
        "total_compared": len(function_results),
        "skipped_no_source": skipped_no_source,
        "skipped_no_inference": skipped_no_inference,
        "aggregate": aggregate,
        "functions": function_results,
    }

    # Write JSON
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(result, indent=2))

    return result


# --------------------------------------------------------------------------- #
#  Summary
# --------------------------------------------------------------------------- #

def print_summary(results, aggregate_fn):
    """Print a summary table to stdout."""
    if not results:
        print("No results to display.")
        return

    print(f"\n{'='*72}")
    print(f"  {'Binary':<40} {'Compared':>8} {'NoSrc':>6} {'NoInf':>6}")
    print(f"  {'-'*40} {'-'*8} {'-'*6} {'-'*6}")

    total_compared = 0
    total_no_source = 0
    total_no_inference = 0
    all_function_results = []

    for r in results:
        label = f"{r['repo']} / {r['binary']}"
        if len(label) > 40:
            label = label[:37] + "..."
        print(f"  {label:<40} {r['total_compared']:>8} {r['skipped_no_source']:>6} {r['skipped_no_inference']:>6}")

        total_compared += r["total_compared"]
        total_no_source += r["skipped_no_source"]
        total_no_inference += r["skipped_no_inference"]
        all_function_results.extend(r["functions"].values())

    print(f"  {'-'*40} {'-'*8} {'-'*6} {'-'*6}")
    print(f"  {'TOTAL':<40} {total_compared:>8} {total_no_source:>6} {total_no_inference:>6}")

    # Overall aggregate
    if results:
        overall = aggregate_fn(all_function_results) if all_function_results else aggregate_fn([])
        print(f"\n  Overall aggregate ({results[0]['metric']}):")
        for k, v in overall.items():
            print(f"    {k}: {v}")

    print(f"{'='*72}")


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    # Two-pass parse: first grab --metric so we can load metric-specific args,
    # then re-parse with the full set.
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--metric", required=True)
    pre_args, _ = pre_parser.parse_known_args()

    try:
        metric_module = get_metric_module(pre_args.metric)
    except ImportError as e:
        print(f"Error loading metric '{pre_args.metric}': {e}")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Compare inference output against original source chunks.",
    )
    parser.add_argument(
        "--metric", required=True,
        help="Name of comparison metric (maps to proj261.eval.comparisons.<metric>)",
    )
    parser.add_argument(
        "--repo", type=str, nargs="*", default=None,
        help="Filter to specific repo(s) (e.g. ollama/ollama)",
    )
    parser.add_argument(
        "--variant", type=str, default=None,
        help="Filter to a specific variant (default, debug, stripped)",
    )
    parser.add_argument(
        "--max-repos", type=int, default=None,
        help="Limit number of repos to process",
    )
    parser.add_argument(
        "--max-binaries", type=int, default=None,
        help="Limit total number of binaries to process",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-evaluate even if results exist",
    )

    # Let the metric add its own CLI flags
    if hasattr(metric_module, "add_args"):
        metric_module.add_args(parser)

    args = parser.parse_args()

    # Let the metric initialize (e.g. create API clients)
    if hasattr(metric_module, "configure"):
        metric_module.configure(args)

    # Load comparator
    try:
        compare_fn, aggregate_fn = get_comparator(args.metric)
    except (ImportError, AttributeError) as e:
        print(f"Error loading metric '{args.metric}': {e}")
        sys.exit(1)

    # Collect entries
    meta = load_metadata()
    entries = collect_entries(meta, args)

    if not entries:
        print("No matching binaries found (need both source and decomp chunks).")
        return

    print(f"Evaluating {len(entries)} binaries with metric={args.metric}")

    results = []
    for entry in tqdm(entries, desc="Evaluating", unit="bin"):
        tqdm.write(f"  {entry['repo']}  {entry['variant']}/{entry['binary']}")
        result = evaluate_binary(
            entry, compare_fn, aggregate_fn,
            args.metric, args.force,
        )
        results.append(result)

    print_summary(results, aggregate_fn)


if __name__ == "__main__":
    main()
