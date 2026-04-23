"""CodeBLEU comparison metric.

Uses the `codebleu` package to compute a weighted combination of n-gram match,
weighted n-gram match, AST match, and data-flow match between the original Go
source and the inferred Go source.
"""

import logging

from codebleu import calc_codebleu

# Suppress noisy dataflow warnings on short/simple functions —
# codebleu logs to the root logger so we raise its level globally.
logging.getLogger().setLevel(logging.ERROR)


def compare_functions(
    source: str,
    inferred: str,
    decomp: str,
    metadata: dict,
) -> dict:
    """Compute CodeBLEU between source and inferred Go code."""
    result = calc_codebleu(
        references=[source],
        predictions=[inferred],
        lang="go",
    )
    return {
        "score": result["codebleu"],
        "ngram_match": result["ngram_match_score"],
        "weighted_ngram_match": result["weighted_ngram_match_score"],
        "syntax_match": result["syntax_match_score"],
        "dataflow_match": result["dataflow_match_score"],
        "source_len": len(source),
    }


def aggregate(results: list[dict]) -> dict:
    """Compute mean and source-length-weighted mean of each CodeBLEU component."""
    if not results:
        return {
            "mean_score": 0.0,
            "mean_ngram_match": 0.0,
            "mean_weighted_ngram_match": 0.0,
            "mean_syntax_match": 0.0,
            "mean_dataflow_match": 0.0,
            "weighted_score": 0.0,
            "weighted_ngram_match": 0.0,
            "weighted_weighted_ngram_match": 0.0,
            "weighted_syntax_match": 0.0,
            "weighted_dataflow_match": 0.0,
        }
    n = len(results)
    with_len = [r for r in results if "source_len" in r]
    fields = ["score", "ngram_match", "weighted_ngram_match", "syntax_match", "dataflow_match"]
    agg = {}
    for f in fields:
        agg[f"mean_{f}"] = round(sum(r[f] for r in results) / n, 4)
        if with_len:
            total_len = sum(r["source_len"] for r in with_len)
            agg[f"weighted_{f}"] = round(sum(r[f] * r["source_len"] for r in with_len) / total_len, 4)
        else:
            agg[f"weighted_{f}"] = agg[f"mean_{f}"]
    return agg
