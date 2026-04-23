"""Syntax validity metric via tree-sitter Go parser.

Checks whether the inferred Go code is syntactically valid by parsing it with
tree-sitter.  The score is 1.0 for a clean parse (no errors) and 0.0 if any
parse errors are found.  Per-function results also include ``error_count``
(number of ERROR / MISSING nodes) and ``node_count`` (total AST nodes) so that
partial-parse quality can be analyzed downstream.
"""

import tree_sitter_go
from tree_sitter import Language, Parser

GO_LANGUAGE = Language(tree_sitter_go.language())


def _make_parser() -> Parser:
    return Parser(GO_LANGUAGE)


def _count_errors(node) -> int:
    """Count ERROR and MISSING nodes in the tree."""
    count = 1 if node.type == "ERROR" or node.is_missing else 0
    for child in node.children:
        count += _count_errors(child)
    return count


def _count_nodes(node) -> int:
    """Count total nodes in the tree."""
    count = 1
    for child in node.children:
        count += _count_nodes(child)
    return count


def compare_functions(
    source: str,
    inferred: str,
    decomp: str,
    metadata: dict,
) -> dict:
    """Check whether *inferred* is syntactically valid Go."""
    parser = _make_parser()
    tree = parser.parse(bytes(inferred, "utf-8"))
    root = tree.root_node

    error_count = _count_errors(root)
    node_count = _count_nodes(root)
    valid = not root.has_error

    return {
        "score": 1.0 if valid else 0.0,
        "valid": valid,
        "error_count": error_count,
        "node_count": node_count,
        "source_len": len(source),
    }


def aggregate(results: list[dict]) -> dict:
    """Report parse success rate and error statistics."""
    if not results:
        return {
            "mean_score": 0.0,
            "weighted_score": 0.0,
            "valid_count": 0,
            "invalid_count": 0,
            "total": 0,
        }

    n = len(results)
    valid = sum(1 for r in results if r.get("valid"))
    mean = valid / n

    with_len = [r for r in results if "source_len" in r]
    if with_len:
        total_len = sum(r["source_len"] for r in with_len)
        weighted = (
            sum(r["score"] * r["source_len"] for r in with_len) / total_len
            if total_len > 0
            else mean
        )
    else:
        weighted = mean

    return {
        "mean_score": round(mean, 4),
        "weighted_score": round(weighted, 4),
        "valid_count": valid,
        "invalid_count": n - valid,
        "total": n,
    }
