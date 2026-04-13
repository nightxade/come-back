"""CodeBLEU comparison metric.

Uses the `codebleu` package to compute a weighted combination of n-gram match,
weighted n-gram match, AST match, and data-flow match between the original Go
source and the inferred Go source.
"""

from codebleu import calc_codebleu


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
    }


def aggregate(results: list[dict]) -> dict:
    """Compute mean of each CodeBLEU component across results."""
    if not results:
        return {
            "mean_score": 0.0,
            "mean_ngram_match": 0.0,
            "mean_weighted_ngram_match": 0.0,
            "mean_syntax_match": 0.0,
            "mean_dataflow_match": 0.0,
        }
    n = len(results)
    return {
        "mean_score": round(sum(r["score"] for r in results) / n, 4),
        "mean_ngram_match": round(sum(r["ngram_match"] for r in results) / n, 4),
        "mean_weighted_ngram_match": round(sum(r["weighted_ngram_match"] for r in results) / n, 4),
        "mean_syntax_match": round(sum(r["syntax_match"] for r in results) / n, 4),
        "mean_dataflow_match": round(sum(r["dataflow_match"] for r in results) / n, 4),
    }
