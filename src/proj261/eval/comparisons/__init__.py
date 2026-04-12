"""Pluggable comparison metrics for evaluation.

Each metric module must define:
    compare_functions(source, inferred, decomp, metadata) -> dict  (with at least "score")
    aggregate(results: list[dict]) -> dict
"""

import importlib


def get_comparator(metric_name: str):
    """Dynamically import proj261.eval.comparisons.<metric_name> and return its callables.

    Returns (compare_functions, aggregate) tuple.
    """
    module = importlib.import_module(f"proj261.eval.comparisons.{metric_name}")
    return module.compare_functions, module.aggregate
