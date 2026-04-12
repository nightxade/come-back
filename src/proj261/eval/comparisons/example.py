"""Placeholder comparison metric for testing the evaluation framework."""


def compare_functions(
    source: str,
    inferred: str,
    decomp: str,
    metadata: dict,
) -> dict:
    """Trivial metric: always returns score 0.0."""
    return {"score": 0.0, "note": "placeholder"}


def aggregate(results: list[dict]) -> dict:
    """Compute mean score across results."""
    if not results:
        return {"mean_score": 0.0}
    return {"mean_score": sum(r["score"] for r in results) / len(results)}
