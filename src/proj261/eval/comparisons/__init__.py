"""Pluggable comparison metrics for evaluation.

Each metric module must define:
    compare_functions(source, inferred, decomp, metadata) -> dict  (with at least "score")
    aggregate(results: list[dict]) -> dict

Optionally, a module may also define:
    add_args(parser)     — add metric-specific CLI arguments
    configure(args)      — called after arg parsing to initialize module state (e.g. API clients)
"""

import importlib


def get_metric_module(metric_name: str):
    """Dynamically import and return the proj261.eval.comparisons.<metric_name> module."""
    return importlib.import_module(f"proj261.eval.comparisons.{metric_name}")


def get_comparator(metric_name: str):
    """Dynamically import proj261.eval.comparisons.<metric_name> and return its callables.

    Returns (compare_functions, aggregate) tuple.
    """
    module = get_metric_module(metric_name)
    return module.compare_functions, module.aggregate
