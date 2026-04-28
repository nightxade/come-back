#!/usr/bin/env python3
"""Compare inference output against original source chunks.

Matches source chunks to inference output by file stem, calls a pluggable
comparison metric, and reports results as JSON + stdout summary.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
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
#  Pending-batch tracker
# --------------------------------------------------------------------------- #

def _pending_path(metric: str) -> Path:
    return RESULTS_DIR / metric / "pending_batches.json"


def _load_pending(metric: str) -> list[dict]:
    p = _pending_path(metric)
    if p.exists():
        return json.loads(p.read_text())
    return []


def _save_pending(metric: str, entries: list[dict]):
    p = _pending_path(metric)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2))


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
                inference_dir = PRED_DIR / sname / variant / bin_name

                # Source chunks, decomp chunks, and predictions must exist
                if not (source_dir / "manifest.json").exists():
                    continue
                if not (decomp_dir / "manifest.json").exists():
                    continue
                if not inference_dir.exists():
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
#  Backfill source_len into cached results
# --------------------------------------------------------------------------- #

def _backfill_source_len(result: dict, source_dir: Path) -> bool:
    """Inject source_len into per-function results that lack it.

    Reads the source .go file for each function stem and sets
    source_len = len(contents).  Returns True if any values were added.
    """
    functions = result.get("functions", {})
    changed = False
    for stem, fres in functions.items():
        if "source_len" in fres:
            continue
        src_file = source_dir / f"{stem}.go"
        if src_file.exists():
            fres["source_len"] = len(src_file.read_text())
            changed = True
    return changed


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
                if _backfill_source_len(existing, source_dir):
                    all_scores = list(existing.get("functions", {}).values())
                    existing["aggregate"] = aggregate_fn(all_scores) if all_scores else aggregate_fn([])
                    results_path.write_text(json.dumps(existing, indent=2))
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
#  Batch evaluation support
# --------------------------------------------------------------------------- #

def _collect_binary_work(entry, metric, force):
    """Gather matchable functions for a binary without running any comparisons.

    Returns ``(binary_meta, work_items)`` where *binary_meta* is a dict with
    the skeleton result (counters, paths) and *work_items* is a list of dicts
    each containing ``key``, ``file_stem``, ``source``, ``inferred``, ready
    for batch submission.

    Returns ``(cached_result, [])`` when a valid cached result exists.
    """
    repo = entry["repo"]
    variant = entry["variant"]
    binary = entry["binary"]
    sname = safe_name(repo)

    source_dir = Path(entry["source_dir"])
    decomp_dir = Path(entry["decomp_dir"])
    inference_dir = PRED_DIR / sname / variant / binary

    results_dir = RESULTS_DIR / metric / sname / variant
    results_path = results_dir / f"{binary}.json"

    # Cache check (same logic as evaluate_binary)
    if not force and results_path.exists():
        try:
            existing = json.loads(results_path.read_text())
            stale = False
            if existing.get("skipped_no_inference", 0) > 0:
                inf_meta = inference_dir / "metadata.json"
                if inf_meta.exists() and inf_meta.stat().st_mtime > results_path.stat().st_mtime:
                    stale = True
            if not stale:
                if _backfill_source_len(existing, source_dir):
                    results_path.write_text(json.dumps(existing, indent=2))
                return existing, []
        except json.JSONDecodeError:
            pass

    source_manifest = json.loads((source_dir / "manifest.json").read_text())
    decomp_manifest = json.loads((decomp_dir / "manifest.json").read_text())

    source_by_stem = {}
    for src_entry in source_manifest["functions"]:
        stem = src_entry["file"].removesuffix(".go")
        source_by_stem[stem] = src_entry

    skipped_no_source = 0
    skipped_no_inference = 0
    work_items = []

    for decomp_entry in decomp_manifest["functions"]:
        file_stem = decomp_entry["file"].removesuffix(".c")

        src_entry = source_by_stem.get(file_stem)
        if src_entry is None:
            skipped_no_source += 1
            continue

        source_file = source_dir / src_entry["file"]
        if not source_file.exists():
            skipped_no_source += 1
            continue
        source_code = source_file.read_text()

        inference_file = inference_dir / f"{file_stem}.go"
        if not inference_file.exists():
            skipped_no_inference += 1
            continue
        inferred_code = inference_file.read_text()

        work_items.append({
            "file_stem": file_stem,
            "source": source_code,
            "inferred": inferred_code,
        })

    binary_meta = {
        "repo": repo,
        "variant": variant,
        "binary": binary,
        "metric": metric,
        "skipped_no_source": skipped_no_source,
        "skipped_no_inference": skipped_no_inference,
        "results_dir": str(results_dir),
        "results_path": str(results_path),
    }

    return binary_meta, work_items


def submit_batch_evaluation(entries, metric_module, metric, force, aggregate_fn=None):
    """Submit a batch job for evaluation (fire-and-forget).

    Collects all work across binaries, calls submit_batch(), and saves
    tracking information to pending_batches.json.  Does NOT wait for
    results.
    """
    submit_fn = metric_module.submit_batch

    # 1. Collect work from all binaries
    print("Collecting work for batch evaluation...")
    cached_results = []
    binary_metas = []       # parallel with work_ranges
    all_work_items = []     # flat list of work items across all binaries
    work_ranges = []        # (start_idx, count) into all_work_items per binary

    for entry in entries:
        meta, items = _collect_binary_work(entry, metric, force)
        if not items:
            cached_results.append(meta)
            continue
        start = len(all_work_items)
        for item in items:
            item["key"] = f"k{len(all_work_items):06d}"
            all_work_items.append(item)
        work_ranges.append((start, len(items)))
        binary_metas.append(meta)

    if not all_work_items:
        print("All binaries cached or have no comparable functions.")
        displayable = [r for r in cached_results if "functions" in r]
        if displayable:
            print_summary(displayable, aggregate_fn)
        return

    print(f"Found {len(binary_metas)} binaries with {len(all_work_items)} "
          f"functions to evaluate ({len(cached_results)} cached).")

    # 2. Submit batch
    job_name, uploaded_file = submit_fn(all_work_items)
    if job_name is None:
        return

    # 3. Build key→file_stem and key→source_len mappings for later reassembly
    key_stems = {}
    key_source_lens = {}
    for item in all_work_items:
        key_stems[item["key"]] = item["file_stem"]
        key_source_lens[item["key"]] = len(item["source"])

    # 4. Save tracking entry
    pending = _load_pending(metric)
    pending.append({
        "job_name": job_name,
        "model": getattr(metric_module, "_model", "unknown"),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "num_items": len(all_work_items),
        "binary_metas": binary_metas,
        "work_ranges": work_ranges,
        "key_stems": key_stems,
        "key_source_lens": key_source_lens,
    })
    _save_pending(metric, pending)

    print(f"\nBatch job submitted: {job_name}")
    print(f"Tracking {len(all_work_items)} comparisons across "
          f"{len(binary_metas)} binaries.")
    print("Run with --retrieve to check results later.")

    displayable = [r for r in cached_results if "functions" in r]
    if displayable and aggregate_fn:
        print(f"\nCached results for {len(displayable)} binary(ies):")
        print_summary(displayable, aggregate_fn)


def retrieve_batch_results(metric_module, metric, aggregate_fn):
    """Check all pending batch jobs and download completed results.

    For each completed job, reassembles per-binary results and writes
    the result JSONs.  Still-running jobs are kept in the tracker.
    """
    retrieve_fn = metric_module.retrieve_batch

    pending = _load_pending(metric)
    if not pending:
        print("No pending batch jobs.")
        return

    print(f"Checking {len(pending)} pending batch job(s)...")
    still_pending = []
    all_results = []

    for entry in pending:
        job_name = entry["job_name"]
        try:
            scores_by_key = retrieve_fn(job_name)
        except RuntimeError as e:
            print(f"  FAILED: {e}")
            # Drop failed/cancelled jobs from tracker
            continue

        if scores_by_key is None:
            # Still running
            still_pending.append(entry)
            continue

        # Reassemble per-binary results
        binary_metas = entry["binary_metas"]
        work_ranges = entry["work_ranges"]
        key_stems = entry["key_stems"]
        key_source_lens = entry.get("key_source_lens", {})

        for bmeta, (start, count) in zip(binary_metas, work_ranges):
            function_results = {}
            for i in range(start, start + count):
                key = f"k{i:06d}"
                stem = key_stems[key]
                result_entry = scores_by_key.get(
                    key, {"score": -1, "error": "missing_from_batch"},
                )
                if key in key_source_lens:
                    result_entry["source_len"] = key_source_lens[key]
                function_results[stem] = result_entry

            all_scores = list(function_results.values())
            agg = aggregate_fn(all_scores) if all_scores else aggregate_fn([])

            result = {
                "repo": bmeta["repo"],
                "variant": bmeta["variant"],
                "binary": bmeta["binary"],
                "metric": metric,
                "total_compared": len(function_results),
                "skipped_no_source": bmeta["skipped_no_source"],
                "skipped_no_inference": bmeta["skipped_no_inference"],
                "aggregate": agg,
                "functions": function_results,
            }

            results_dir = Path(bmeta["results_dir"])
            results_path = Path(bmeta["results_path"])
            results_dir.mkdir(parents=True, exist_ok=True)
            results_path.write_text(json.dumps(result, indent=2))
            all_results.append(result)

        print(f"  {job_name}: completed, wrote {len(binary_metas)} result file(s).")

    _save_pending(metric, still_pending)

    if still_pending:
        print(f"\n{len(still_pending)} job(s) still running.")
    if all_results:
        print(f"{len(all_results)} binary result(s) written.")
        print_summary(all_results, aggregate_fn)


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    # Two-pass parse: first grab --metric so we can load metric-specific args,
    # then re-parse with the full set.
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--metric", default=None)
    pre_args, remaining = pre_parser.parse_known_args()

    parser = argparse.ArgumentParser(
        description="Compare inference output against original source chunks.",
    )

    metric_module = None
    if pre_args.metric:
        try:
            metric_module = get_metric_module(pre_args.metric)
        except ImportError as e:
            print(f"Error loading metric '{pre_args.metric}': {e}")
            sys.exit(1)
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
    parser.add_argument(
        "--retrieve", action="store_true",
        help="Check pending batch jobs and download completed results",
    )

    # Let the metric add its own CLI flags
    if metric_module and hasattr(metric_module, "add_args"):
        metric_module.add_args(parser)

    args = parser.parse_args()

    if not args.metric:
        parser.error("the following arguments are required: --metric")

    # Load metric module if not already loaded (shouldn't happen, but just in case)
    if metric_module is None:
        try:
            metric_module = get_metric_module(args.metric)
        except ImportError as e:
            print(f"Error loading metric '{args.metric}': {e}")
            sys.exit(1)

    # Let the metric initialize (e.g. create API clients)
    if hasattr(metric_module, "configure"):
        metric_module.configure(args)

    # Load comparator
    try:
        compare_fn, aggregate_fn = get_comparator(args.metric)
    except (ImportError, AttributeError) as e:
        print(f"Error loading metric '{args.metric}': {e}")
        sys.exit(1)

    # --retrieve mode: check pending jobs, download completed results
    if args.retrieve:
        retrieve_batch_results(metric_module, args.metric, aggregate_fn)
        return

    # Collect entries
    meta = load_metadata()
    entries = collect_entries(meta, args)

    if not entries:
        print("No matching binaries found (need both source and decomp chunks).")
        return

    print(f"Evaluating {len(entries)} binaries with metric={args.metric}")

    # Use batch mode if the metric supports it and it wasn't disabled
    use_batch = (hasattr(metric_module, "use_batch")
                 and metric_module.use_batch()
                 and hasattr(metric_module, "submit_batch"))

    if use_batch:
        submit_batch_evaluation(
            entries, metric_module, args.metric, args.force,
            aggregate_fn=aggregate_fn,
        )
    else:
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
