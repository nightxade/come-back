"""Placeholder comparison metric for testing the evaluation framework."""


def compare_functions(
    source: str,
    inferred: str,
    decomp: str,
    metadata: dict,
) -> dict:
    """Trivial metric: always returns score 0.0."""
    return {"score": 0.0, "note": "placeholder", "source_len": len(source)}


def aggregate(results: list[dict]) -> dict:
    """Compute mean and source-length-weighted mean score across results."""
    if not results:
        return {"mean_score": 0.0, "weighted_score": 0.0}
    n = len(results)
    mean = sum(r["score"] for r in results) / n
    with_len = [r for r in results if "source_len" in r]
    if with_len:
        total_len = sum(r["source_len"] for r in with_len)
        weighted = sum(r["score"] * r["source_len"] for r in with_len) / total_len
    else:
        weighted = mean
    return {"mean_score": round(mean, 4), "weighted_score": round(weighted, 4)}
