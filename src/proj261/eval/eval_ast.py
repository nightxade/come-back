#!/usr/bin/env python3
"""AST-binned evaluation statistics.

Reads existing result JSONs, parses the corresponding Go source chunks with
tree-sitter, computes AST node count and depth for each function, bins the
results, and displays per-bin statistics.
"""

import argparse
import json
import sys
from pathlib import Path

import tree_sitter_go
from tree_sitter import Language, Parser

from proj261.eval.compare import (
    RESULTS_DIR,
    collect_entries,
    load_metadata,
)
from proj261.util import CHUNKED_SOURCES_DIR, safe_name


# --------------------------------------------------------------------------- #
#  AST metrics
# --------------------------------------------------------------------------- #

GO_LANGUAGE = Language(tree_sitter_go.language())


def _make_parser() -> Parser:
    return Parser(GO_LANGUAGE)


def _compute_ast_metrics(source: str) -> tuple[int, int]:
    """Parse Go source and return (node_count, max_depth)."""
    parser = _make_parser()
    tree = parser.parse(bytes(source, "utf-8"))

    def _walk(node, depth):
        count = 1
        max_d = depth
        for child in node.children:
            c, d = _walk(child, depth + 1)
            count += c
            if d > max_d:
                max_d = d
        return count, max_d

    node_count, max_depth = _walk(tree.root_node, 1)
    return node_count, max_depth


# --------------------------------------------------------------------------- #
#  Binning
# --------------------------------------------------------------------------- #

SIZE_BINS = [(1, 25), (25, 50), (50, 100), (100, 200), (200, 500), (500, float("inf"))]
DEPTH_BINS = [(1, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, float("inf"))]


def _bin_label(lo, hi):
    hi_str = "inf" if hi == float("inf") else str(int(hi))
    return f"[{int(lo)}, {hi_str})"


def _assign_bin(value, bins):
    for lo, hi in bins:
        if lo <= value < hi:
            return (lo, hi)
    return bins[-1]


def _compute_bin_stats(records, bins):
    """Compute per-bin statistics.

    *records* is a list of (bin_value, score, source_len) tuples.
    Returns a list of (label, count, mean_score, weighted_score) tuples
    plus a total row.
    """
    from collections import defaultdict

    buckets = defaultdict(list)  # bin_key -> [(score, source_len)]
    for val, score, src_len in records:
        key = _assign_bin(val, bins)
        buckets[key].append((score, src_len))

    rows = []
    total_count = 0
    total_score_sum = 0.0
    total_weighted_num = 0.0
    total_weighted_den = 0.0

    for lo, hi in bins:
        items = buckets.get((lo, hi), [])
        count = len(items)
        if count == 0:
            rows.append((_bin_label(lo, hi), 0, None, None))
            continue

        scores = [s for s, _ in items]
        mean_score = sum(scores) / count

        weights = [sl for _, sl in items]
        w_total = sum(weights)
        weighted_score = (
            sum(s * w for s, w in zip(scores, weights)) / w_total
            if w_total > 0
            else mean_score
        )

        rows.append((_bin_label(lo, hi), count, mean_score, weighted_score))
        total_count += count
        total_score_sum += sum(scores)
        total_weighted_num += sum(s * w for s, w in zip(scores, weights))
        total_weighted_den += w_total

    # Total row
    total_mean = total_score_sum / total_count if total_count else None
    total_weighted = (
        total_weighted_num / total_weighted_den
        if total_weighted_den > 0
        else total_mean
    )
    rows.append(("TOTAL", total_count, total_mean, total_weighted))
    return rows


# --------------------------------------------------------------------------- #
#  Output
# --------------------------------------------------------------------------- #

def _print_table(title, rows):
    print(f"\n{title}")
    print(f"  {'Bin':<18} {'Count':>6}  {'Mean Score':>10}  {'Weighted Score':>14}")
    print(f"  {'─' * 18} {'─' * 6}  {'─' * 10}  {'─' * 14}")
    for label, count, mean, weighted in rows:
        mean_s = f"{mean:.4f}" if mean is not None else "—"
        weighted_s = f"{weighted:.4f}" if weighted is not None else "—"
        if label == "TOTAL":
            print(f"  {'─' * 18} {'─' * 6}  {'─' * 10}  {'─' * 14}")
        print(f"  {label:<18} {count:>6}  {mean_s:>10}  {weighted_s:>14}")


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Show evaluation results binned by Go source AST complexity.",
    )
    parser.add_argument(
        "--metric", required=True,
        help="Name of comparison metric (e.g. llm, codebleu)",
    )
    parser.add_argument(
        "--repo", type=str, nargs="*", default=None,
        help="Filter to specific repo(s)",
    )
    parser.add_argument(
        "--variant", type=str, default=None,
        help="Filter to a specific variant",
    )
    parser.add_argument(
        "--max-repos", type=int, default=None,
        help="Limit number of repos to process",
    )
    parser.add_argument(
        "--max-binaries", type=int, default=None,
        help="Limit total number of binaries to process",
    )
    args = parser.parse_args()

    meta = load_metadata()
    entries = collect_entries(meta, args)

    if not entries:
        print("No matching binaries found.")
        sys.exit(1)

    # Collect (ast_size, ast_depth, score, source_len) per function
    size_records = []   # (ast_size, score, source_len)
    depth_records = []  # (ast_depth, score, source_len)
    total_funcs = 0
    skipped_no_result = 0
    skipped_no_source = 0

    for entry in entries:
        repo = entry["repo"]
        variant = entry["variant"]
        binary = entry["binary"]
        sname = safe_name(repo)

        result_path = RESULTS_DIR / args.metric / sname / variant / f"{binary}.json"
        if not result_path.exists():
            skipped_no_result += 1
            continue

        result = json.loads(result_path.read_text())
        source_dir = CHUNKED_SOURCES_DIR / sname / variant / binary

        for stem, fres in result.get("functions", {}).items():
            score = fres.get("score")
            if score is None or score < 0:
                continue

            source_len = fres.get("source_len", 0)
            src_file = source_dir / f"{stem}.go"
            if not src_file.exists():
                skipped_no_source += 1
                continue

            source_code = src_file.read_text()
            node_count, max_depth = _compute_ast_metrics(source_code)

            size_records.append((node_count, score, source_len))
            depth_records.append((max_depth, score, source_len))
            total_funcs += 1

    if not size_records:
        print("No functions with valid scores and source files found.")
        sys.exit(1)

    print(f"\nAnalyzed {total_funcs} functions across {len(entries)} binaries.")
    if skipped_no_result:
        print(f"  ({skipped_no_result} binaries had no result JSON)")
    if skipped_no_source:
        print(f"  ({skipped_no_source} functions had no source .go file)")

    size_rows = _compute_bin_stats(size_records, SIZE_BINS)
    depth_rows = _compute_bin_stats(depth_records, DEPTH_BINS)

    _print_table(f"AST Node Count Bins (metric: {args.metric})", size_rows)
    _print_table(f"\nAST Depth Bins (metric: {args.metric})", depth_rows)
    print()


if __name__ == "__main__":
    main()
