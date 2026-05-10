#!/usr/bin/env python3
"""Comprehensive statistical analysis of evaluation results.

Loads all result JSONs across metrics (llm, codebleu, syntax) and variants
(default, debug, stripped), builds a unified DataFrame, computes statistics,
and generates plots.  All output goes to the ``statistics/`` directory.
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats as sp_stats

from proj261.eval.compare import RESULTS_DIR, collect_entries, load_metadata
from proj261.eval.eval_ast import (
    DEPTH_BINS,
    SIZE_BINS,
    _assign_bin,
    _bin_label,
    _compute_ast_metrics,
)
from proj261.util import CHUNKED_SOURCES_DIR, PROJECT_DIR, safe_name

METRICS = ["llm", "codebleu", "syntax"]
VARIANTS = ["default", "debug", "stripped"]
STATS_DIR = PROJECT_DIR / "statistics"


# --------------------------------------------------------------------------- #
#  Data loading
# --------------------------------------------------------------------------- #

def _load_all_results(entries: list[dict]) -> pd.DataFrame:
    """Load result JSONs for all metrics and build a unified DataFrame.

    One row per (repo, variant, binary, stem) with columns for each metric's
    scores, plus source_len and AST metrics from the source file.
    """
    # Accumulate per-function records keyed by (repo, variant, binary, stem).
    # Each record merges data across metrics.
    records: dict[tuple, dict] = {}

    for entry in entries:
        repo = entry["repo"]
        variant = entry["variant"]
        binary = entry["binary"]
        sname = safe_name(repo)
        source_dir = CHUNKED_SOURCES_DIR / sname / variant / binary

        for metric in METRICS:
            result_path = (
                RESULTS_DIR / metric / sname / variant / f"{binary}.json"
            )
            if not result_path.exists():
                continue

            result = json.loads(result_path.read_text())
            for stem, fres in result.get("functions", {}).items():
                score = fres.get("score")
                if score is None or score < 0:
                    continue

                key = (repo, variant, binary, stem)
                if key not in records:
                    records[key] = {
                        "repo": repo,
                        "variant": variant,
                        "binary": binary,
                        "stem": stem,
                        "source_len": fres.get("source_len", np.nan),
                        "ast_node_count": np.nan,
                        "ast_depth": np.nan,
                    }

                rec = records[key]

                # Metric scores
                if metric == "llm":
                    rec["llm_score"] = score
                elif metric == "codebleu":
                    rec["codebleu_score"] = score
                    rec["codebleu_ngram"] = fres.get("ngram_match", np.nan)
                    rec["codebleu_weighted_ngram"] = fres.get(
                        "weighted_ngram_match", np.nan
                    )
                    rec["codebleu_syntax_match"] = fres.get(
                        "syntax_match", np.nan
                    )
                    rec["codebleu_dataflow"] = fres.get(
                        "dataflow_match", np.nan
                    )
                elif metric == "syntax":
                    rec["syntax_score"] = score
                    rec["syntax_valid"] = fres.get("valid")
                    rec["syntax_error_count"] = fres.get("error_count", np.nan)
                    rec["syntax_node_count"] = fres.get("node_count", np.nan)

                # Update source_len if we got a better value
                sl = fres.get("source_len")
                if sl is not None and pd.isna(rec.get("source_len", np.nan)):
                    rec["source_len"] = sl

        # Compute AST metrics from source files for functions in this binary
        for key, rec in records.items():
            if key[0] != repo or key[1] != variant or key[2] != binary:
                continue
            if not pd.isna(rec["ast_node_count"]):
                continue
            src_file = source_dir / f"{rec['stem']}.go"
            if src_file.exists():
                source_code = src_file.read_text()
                nc, depth = _compute_ast_metrics(source_code)
                rec["ast_node_count"] = nc
                rec["ast_depth"] = depth

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(list(records.values()))
    return df


# --------------------------------------------------------------------------- #
#  Text summary
# --------------------------------------------------------------------------- #

def _write_summary(df: pd.DataFrame, out: Path):
    lines: list[str] = []

    def _w(s=""):
        lines.append(s)

    _w("=" * 72)
    _w("  Statistical Summary")
    _w("=" * 72)

    # --- Section A: Dataset overview ---
    _w()
    _w("A. Dataset Overview")
    _w("-" * 40)
    _w(f"  Total unique repos:      {df['repo'].nunique()}")
    _w(f"  Total unique binaries:   {df[['repo', 'binary']].drop_duplicates().shape[0]}")
    for v in VARIANTS:
        vdf = df[df["variant"] == v]
        _w(f"  Functions ({v:>8s}):   {len(vdf)}")
    _w()
    for m in METRICS:
        col = f"{m}_score"
        if col in df.columns:
            valid = df[col].notna().sum()
            _w(f"  Functions with valid {m} score: {valid}")

    # --- Section B: Per-variant aggregate scores ---
    _w()
    _w("B. Per-Variant Aggregate Scores")
    _w("-" * 40)
    for m in METRICS:
        col = f"{m}_score"
        if col not in df.columns:
            continue
        _w(f"\n  Metric: {m}")
        _w(f"  {'Variant':<10s} {'Mean':>8s} {'WtMean':>8s} {'Std':>8s} "
           f"{'Median':>8s} {'Q1':>8s} {'Q3':>8s} {'N':>6s}")
        _w(f"  {'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*6}")
        for v in VARIANTS:
            vdf = df[(df["variant"] == v) & df[col].notna()]
            scores = vdf[col]
            if len(scores) == 0:
                _w(f"  {v:<10s} {'—':>8s} {'—':>8s} {'—':>8s} {'—':>8s} {'—':>8s} {'—':>8s} {0:>6d}")
                continue
            src_lens = vdf["source_len"].fillna(1)
            w_total = src_lens.sum()
            wmean = (scores * src_lens).sum() / w_total if w_total > 0 else scores.mean()
            _w(f"  {v:<10s} {scores.mean():>8.4f} {wmean:>8.4f} {scores.std():>8.4f} "
               f"{scores.median():>8.4f} {scores.quantile(0.25):>8.4f} "
               f"{scores.quantile(0.75):>8.4f} {len(scores):>6d}")

    # --- Section C: Variant comparison (Wilcoxon signed-rank) ---
    _w()
    _w("C. Variant Comparison (Wilcoxon Signed-Rank Test)")
    _w("-" * 40)
    pairs = [("default", "debug"), ("default", "stripped"), ("debug", "stripped")]
    for m in METRICS:
        col = f"{m}_score"
        if col not in df.columns:
            continue
        _w(f"\n  Metric: {m}")
        for v1, v2 in pairs:
            df1 = df[df["variant"] == v1][["repo", "binary", "stem", col]].dropna()
            df2 = df[df["variant"] == v2][["repo", "binary", "stem", col]].dropna()
            merged = df1.merge(df2, on=["repo", "binary", "stem"], suffixes=("_1", "_2"))
            n = len(merged)
            if n < 10:
                _w(f"    {v1} vs {v2}: insufficient paired samples (n={n})")
                continue
            s1 = merged[f"{col}_1"].values
            s2 = merged[f"{col}_2"].values
            diff = s1 - s2
            # Remove zero differences for Wilcoxon
            nonzero = diff != 0
            if nonzero.sum() < 10:
                _w(f"    {v1} vs {v2}: too few non-zero differences (n={nonzero.sum()})")
                continue
            stat, pval = sp_stats.wilcoxon(s1[nonzero], s2[nonzero])
            higher = v1 if np.mean(diff) > 0 else v2
            _w(f"    {v1} vs {v2}: W={stat:.1f}, p={pval:.4e}, n={n}, "
               f"higher={higher}, mean_diff={np.mean(diff):+.4f}")

    # --- Section D: AST-complexity binned scores ---
    _w()
    _w("D. AST-Complexity Binned Scores (all variants)")
    _w("-" * 40)
    for m in METRICS:
        col = f"{m}_score"
        if col not in df.columns:
            continue
        sub = df[df[col].notna() & df["ast_node_count"].notna()].copy()
        if sub.empty:
            continue

        _w(f"\n  Metric: {m}")

        # Size bins
        _w(f"\n  AST Node Count Bins:")
        _w(f"    {'Bin':<18s} {'Count':>6s} {'Mean':>8s} {'WtMean':>8s}")
        _w(f"    {'-'*18} {'-'*6} {'-'*8} {'-'*8}")
        for lo, hi in SIZE_BINS:
            mask = sub["ast_node_count"].apply(lambda x, lo=lo, hi=hi: lo <= x < hi)
            bdf = sub[mask]
            if bdf.empty:
                _w(f"    {_bin_label(lo, hi):<18s} {0:>6d} {'—':>8s} {'—':>8s}")
                continue
            scores = bdf[col]
            src_lens = bdf["source_len"].fillna(1)
            w_total = src_lens.sum()
            wmean = (scores * src_lens).sum() / w_total if w_total > 0 else scores.mean()
            _w(f"    {_bin_label(lo, hi):<18s} {len(bdf):>6d} {scores.mean():>8.4f} {wmean:>8.4f}")

        # Depth bins
        _w(f"\n  AST Depth Bins:")
        _w(f"    {'Bin':<18s} {'Count':>6s} {'Mean':>8s} {'WtMean':>8s}")
        _w(f"    {'-'*18} {'-'*6} {'-'*8} {'-'*8}")
        for lo, hi in DEPTH_BINS:
            mask = sub["ast_depth"].apply(lambda x, lo=lo, hi=hi: lo <= x < hi)
            bdf = sub[mask]
            if bdf.empty:
                _w(f"    {_bin_label(lo, hi):<18s} {0:>6d} {'—':>8s} {'—':>8s}")
                continue
            scores = bdf[col]
            src_lens = bdf["source_len"].fillna(1)
            w_total = src_lens.sum()
            wmean = (scores * src_lens).sum() / w_total if w_total > 0 else scores.mean()
            _w(f"    {_bin_label(lo, hi):<18s} {len(bdf):>6d} {scores.mean():>8.4f} {wmean:>8.4f}")

    # --- Section E: Metric correlations ---
    _w()
    _w("E. Metric Correlations")
    _w("-" * 40)

    # llm vs codebleu
    if "llm_score" in df.columns and "codebleu_score" in df.columns:
        sub = df[["llm_score", "codebleu_score"]].dropna()
        if len(sub) >= 10:
            pr, pp = sp_stats.pearsonr(sub["llm_score"], sub["codebleu_score"])
            sr, sp = sp_stats.spearmanr(sub["llm_score"], sub["codebleu_score"])
            _w(f"  llm_score vs codebleu_score (n={len(sub)}):")
            _w(f"    Pearson  r={pr:.4f}, p={pp:.4e}")
            _w(f"    Spearman r={sr:.4f}, p={sp:.4e}")

    # source_len vs each metric
    for m in METRICS:
        col = f"{m}_score"
        if col not in df.columns:
            continue
        sub = df[["source_len", col]].dropna()
        if len(sub) >= 10:
            pr, pp = sp_stats.pearsonr(sub["source_len"], sub[col])
            sr, sp = sp_stats.spearmanr(sub["source_len"], sub[col])
            _w(f"\n  source_len vs {col} (n={len(sub)}):")
            _w(f"    Pearson  r={pr:.4f}, p={pp:.4e}")
            _w(f"    Spearman r={sr:.4f}, p={sp:.4e}")

    # ast_node_count vs each metric
    for m in METRICS:
        col = f"{m}_score"
        if col not in df.columns:
            continue
        sub = df[["ast_node_count", col]].dropna()
        if len(sub) >= 10:
            pr, pp = sp_stats.pearsonr(sub["ast_node_count"], sub[col])
            sr, sp = sp_stats.spearmanr(sub["ast_node_count"], sub[col])
            _w(f"\n  ast_node_count vs {col} (n={len(sub)}):")
            _w(f"    Pearson  r={pr:.4f}, p={pp:.4e}")
            _w(f"    Spearman r={sr:.4f}, p={sp:.4e}")

    # --- Section F: Syntax validity ---
    _w()
    _w("F. Syntax Validity")
    _w("-" * 40)
    if "syntax_valid" in df.columns:
        for v in VARIANTS:
            vdf = df[(df["variant"] == v) & df["syntax_valid"].notna()]
            if vdf.empty:
                continue
            valid = vdf["syntax_valid"].sum()
            total = len(vdf)
            _w(f"  {v:<10s}: {int(valid)} / {total} valid ({100*valid/total:.1f}%)")
        allv = df[df["syntax_valid"].notna()]
        if not allv.empty:
            valid = allv["syntax_valid"].sum()
            total = len(allv)
            _w(f"  {'overall':<10s}: {int(valid)} / {total} valid ({100*valid/total:.1f}%)")
    else:
        _w("  No syntax results found.")

    _w()
    _w("=" * 72)

    out.write_text("\n".join(lines) + "\n")
    print(f"Written summary to {out}")


# --------------------------------------------------------------------------- #
#  Plots
# --------------------------------------------------------------------------- #

def _setup_style():
    plt.rcParams.update({
        "figure.dpi": 150,
        "savefig.dpi": 300,
        "font.size": 10,
        "axes.grid": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def _plot_variant_scores(df: pd.DataFrame, out: Path):
    """Grouped bar chart: mean score per variant for llm and codebleu."""
    score_cols = [c for c in ["llm_score", "codebleu_score"] if c in df.columns]
    if not score_cols:
        return

    fig, ax = plt.subplots(figsize=(6, 4))
    x = np.arange(len(VARIANTS))
    width = 0.8 / len(score_cols)

    for i, col in enumerate(score_cols):
        means = []
        stds = []
        for v in VARIANTS:
            vdf = df[(df["variant"] == v) & df[col].notna()]
            means.append(vdf[col].mean() if len(vdf) > 0 else 0)
            stds.append(vdf[col].std() if len(vdf) > 0 else 0)
        offset = (i - (len(score_cols) - 1) / 2) * width
        label = col.replace("_score", "")
        ax.bar(x + offset, means, width, yerr=stds, label=label, capsize=3)

    ax.set_xticks(x)
    ax.set_xticklabels(VARIANTS)
    ax.set_ylabel("Mean Score")
    ax.set_title("Mean Scores by Variant")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_score_distributions(df: pd.DataFrame, out: Path):
    """Box plots: one subplot per metric, variants as categories."""
    score_cols = [c for c in ["llm_score", "codebleu_score", "syntax_score"]
                  if c in df.columns]
    if not score_cols:
        return

    fig, axes = plt.subplots(1, len(score_cols), figsize=(4 * len(score_cols), 4),
                             squeeze=False)
    axes = axes[0]

    for ax, col in zip(axes, score_cols):
        data = []
        labels = []
        for v in VARIANTS:
            vdf = df[(df["variant"] == v) & df[col].notna()]
            if not vdf.empty:
                data.append(vdf[col].values)
                labels.append(v)
        if data:
            bp = ax.boxplot(data, labels=labels, patch_artist=True)
            for patch in bp["boxes"]:
                patch.set_facecolor("#b3cde3")
        ax.set_title(col.replace("_score", ""))
        ax.set_ylabel("Score")

    fig.suptitle("Score Distributions by Variant", y=1.02)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_ast_vs_score(df: pd.DataFrame, bins, col_name: str, xlabel: str,
                       out: Path):
    """Line plot: bin midpoints on x-axis, mean score per metric on y-axis."""
    score_cols = [c for c in ["llm_score", "codebleu_score", "syntax_score"]
                  if c in df.columns]
    if not score_cols or col_name not in df.columns:
        return

    sub = df[df[col_name].notna()].copy()
    if sub.empty:
        return

    fig, ax = plt.subplots(figsize=(6, 4))

    for scol in score_cols:
        xs = []
        ys = []
        for lo, hi in bins:
            mask = sub[col_name].apply(lambda x, lo=lo, hi=hi: lo <= x < hi)
            bdf = sub[mask & sub[scol].notna()]
            if bdf.empty:
                continue
            mid = lo + (min(hi, 1000) - lo) / 2
            xs.append(mid)
            ys.append(bdf[scol].mean())
        label = scol.replace("_score", "")
        ax.plot(xs, ys, marker="o", label=label)

    ax.set_xlabel(xlabel)
    ax.set_ylabel("Mean Score")
    ax.set_title(f"Score vs {xlabel}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_metric_correlation(df: pd.DataFrame, out: Path):
    """2D histogram: llm_score vs codebleu_score with log color scale."""
    if "llm_score" not in df.columns or "codebleu_score" not in df.columns:
        return

    sub = df[["llm_score", "codebleu_score"]].dropna()
    if len(sub) < 10:
        return

    from matplotlib.colors import LogNorm

    fig, ax = plt.subplots(figsize=(5, 5))
    h = ax.hist2d(
        sub["llm_score"], sub["codebleu_score"],
        bins=50, cmap="YlOrRd", norm=LogNorm(), cmin=1,
    )
    fig.colorbar(h[3], ax=ax, label="Count (log scale)")

    r, _ = sp_stats.pearsonr(sub["llm_score"], sub["codebleu_score"])
    ax.annotate(f"Pearson r = {r:.3f}", xy=(0.05, 0.95),
                xycoords="axes fraction", fontsize=10,
                verticalalignment="top",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8))

    ax.set_xlabel("LLM Score")
    ax.set_ylabel("CodeBLEU Score")
    ax.set_title("LLM vs CodeBLEU Score")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_source_len_vs_score(df: pd.DataFrame, out: Path):
    """Binned line plot: mean score vs log-spaced source length bins."""
    score_cols = [c for c in ["llm_score", "codebleu_score", "syntax_score"]
                  if c in df.columns]
    if not score_cols or "source_len" not in df.columns:
        return

    sub = df[df["source_len"].notna() & (df["source_len"] > 0)].copy()
    if sub.empty:
        return

    # Create log-spaced bins
    bin_edges = [0, 50, 100, 200, 500, 1000, 2000, 5000, 10000, float("inf")]
    bin_labels = ["<50", "50-100", "100-200", "200-500", "500-1K",
                  "1K-2K", "2K-5K", "5K-10K", "10K+"]

    sub["len_bin"] = pd.cut(
        sub["source_len"], bins=bin_edges, labels=bin_labels, right=False,
    )

    fig, ax = plt.subplots(figsize=(8, 4))

    for col in score_cols:
        means = sub.groupby("len_bin", observed=True)[col].mean()
        label = col.replace("_score", "")
        ax.plot(range(len(means)), means.values, marker="o", label=label)

    ax.set_xticks(range(len(bin_labels)))
    ax.set_xticklabels(bin_labels, rotation=30, ha="right", fontsize=8)
    ax.set_xlabel("Source Length (chars)")
    ax.set_ylabel("Mean Score")
    ax.set_title("Score vs Source Length")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_score_cdfs(df: pd.DataFrame, out: Path):
    """CDF plots: one subplot per metric, one line per variant."""
    score_cols = [c for c in ["llm_score", "codebleu_score", "syntax_score"]
                  if c in df.columns]
    if not score_cols:
        return

    fig, axes = plt.subplots(1, len(score_cols), figsize=(4 * len(score_cols), 4),
                             squeeze=False)
    axes = axes[0]

    for ax, col in zip(axes, score_cols):
        for v in VARIANTS:
            vals = df[(df["variant"] == v) & df[col].notna()][col].values
            if len(vals) == 0:
                continue
            sorted_vals = np.sort(vals)
            cdf = np.arange(1, len(sorted_vals) + 1) / len(sorted_vals)
            ax.plot(sorted_vals, cdf, label=v, linewidth=1.2)
        ax.set_xlabel("Score")
        ax.set_ylabel("CDF")
        ax.set_title(col.replace("_score", ""))
        ax.legend(fontsize=8)

    fig.suptitle("Cumulative Distribution of Scores", y=1.02)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_codebleu_submetrics(df: pd.DataFrame, out: Path):
    """Grouped bar chart of CodeBLEU sub-components per variant."""
    sub_cols = ["codebleu_ngram", "codebleu_weighted_ngram",
                "codebleu_syntax_match", "codebleu_dataflow"]
    present = [c for c in sub_cols if c in df.columns]
    if not present:
        return

    fig, ax = plt.subplots(figsize=(8, 4))
    x = np.arange(len(VARIANTS))
    width = 0.8 / len(present)

    for i, col in enumerate(present):
        means = []
        for v in VARIANTS:
            vdf = df[(df["variant"] == v) & df[col].notna()]
            means.append(vdf[col].mean() if len(vdf) > 0 else 0)
        offset = (i - (len(present) - 1) / 2) * width
        label = col.replace("codebleu_", "").replace("_", " ")
        ax.bar(x + offset, means, width, label=label)

    ax.set_xticks(x)
    ax.set_xticklabels(VARIANTS)
    ax.set_ylabel("Mean Score")
    ax.set_title("CodeBLEU Sub-Metric Breakdown by Variant")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_variant_scores_weighted(df: pd.DataFrame, out: Path):
    """Grouped bar chart: source-length-weighted mean score per variant."""
    score_cols = [c for c in ["llm_score", "codebleu_score"] if c in df.columns]
    if not score_cols:
        return

    fig, ax = plt.subplots(figsize=(6, 4))
    x = np.arange(len(VARIANTS))
    width = 0.8 / len(score_cols)

    for i, col in enumerate(score_cols):
        wmeans = []
        for v in VARIANTS:
            vdf = df[(df["variant"] == v) & df[col].notna()]
            if len(vdf) == 0:
                wmeans.append(0)
                continue
            src_lens = vdf["source_len"].fillna(1)
            w_total = src_lens.sum()
            wmean = (vdf[col] * src_lens).sum() / w_total if w_total > 0 else vdf[col].mean()
            wmeans.append(wmean)
        offset = (i - (len(score_cols) - 1) / 2) * width
        label = col.replace("_score", "")
        ax.bar(x + offset, wmeans, width, label=label)

    ax.set_xticks(x)
    ax.set_xticklabels(VARIANTS)
    ax.set_ylabel("Weighted Mean Score")
    ax.set_title("Source-Length-Weighted Mean Scores by Variant")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_repo_scores(df: pd.DataFrame, out: Path, top_n: int = 15):
    """Horizontal bar chart: top and bottom repos by mean LLM score."""
    if "llm_score" not in df.columns:
        return

    # Compute per-repo mean across all variants
    repo_scores = (
        df[df["llm_score"].notna()]
        .groupby("repo")
        .agg(
            llm_mean=("llm_score", "mean"),
            codebleu_mean=("codebleu_score", "mean"),
            count=("llm_score", "size"),
        )
    )

    # Require at least 20 functions to avoid noisy single-function repos
    repo_scores = repo_scores[repo_scores["count"] >= 20]

    if len(repo_scores) < 2:
        return

    repo_scores = repo_scores.sort_values("llm_mean")

    # Take bottom and top N
    n = min(top_n, len(repo_scores) // 2)
    if n < 1:
        return
    bottom = repo_scores.head(n)
    top = repo_scores.tail(n)
    selected = pd.concat([bottom, top])

    # Shorten repo names (owner/repo)
    labels = [r.split("/")[-1] if "/" in r else r for r in selected.index]

    fig, axes = plt.subplots(1, 2, figsize=(12, max(5, n * 0.4)), sharey=True)

    y = np.arange(len(selected))

    # LLM scores
    colors = ["#e74c3c" if i < n else "#2ecc71" for i in range(len(selected))]
    axes[0].barh(y, selected["llm_mean"], color=colors, edgecolor="white", linewidth=0.5)
    axes[0].set_yticks(y)
    axes[0].set_yticklabels(labels, fontsize=8)
    axes[0].set_xlabel("Mean LLM Score")
    axes[0].set_title("LLM-as-a-Judge")
    axes[0].set_xlim(0, 1)

    # CodeBLEU scores
    axes[1].barh(y, selected["codebleu_mean"], color=colors, edgecolor="white", linewidth=0.5)
    axes[1].set_xlabel("Mean CodeBLEU Score")
    axes[1].set_title("CodeBLEU")
    axes[1].set_xlim(0, 1)

    fig.suptitle(f"Top and Bottom {n} Repositories by Mean Score", y=1.02)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out.name}")


def _plot_paired_diffs(df: pd.DataFrame, out: Path):
    """Histograms of paired score differences (default - stripped) per metric."""
    score_cols = [c for c in ["llm_score", "codebleu_score", "syntax_score"]
                  if c in df.columns]
    if not score_cols:
        return

    fig, axes = plt.subplots(1, len(score_cols), figsize=(4 * len(score_cols), 4),
                             squeeze=False)
    axes = axes[0]

    v1, v2 = "default", "stripped"
    for ax, col in zip(axes, score_cols):
        df1 = df[df["variant"] == v1][["repo", "binary", "stem", col]].dropna()
        df2 = df[df["variant"] == v2][["repo", "binary", "stem", col]].dropna()
        merged = df1.merge(df2, on=["repo", "binary", "stem"], suffixes=("_1", "_2"))
        if merged.empty:
            continue
        diff = merged[f"{col}_1"].values - merged[f"{col}_2"].values
        ax.hist(diff, bins=50, edgecolor="black", linewidth=0.3, alpha=0.8)
        ax.axvline(0, color="red", linestyle="--", linewidth=0.8)
        mean_d = np.mean(diff)
        ax.axvline(mean_d, color="blue", linestyle="-", linewidth=0.8)
        ax.annotate(f"mean = {mean_d:+.4f}", xy=(0.95, 0.95),
                    xycoords="axes fraction", fontsize=8, ha="right", va="top",
                    bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8))
        ax.set_xlabel(f"Score Diff ({v1} - {v2})")
        ax.set_ylabel("Count")
        ax.set_title(col.replace("_score", ""))

    fig.suptitle(f"Paired Score Differences ({v1} vs {v2})", y=1.02)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out.name}")


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Generate comprehensive statistical analysis of evaluation results.",
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

    # Load metadata and collect entries
    meta = load_metadata()
    entries = collect_entries(meta, args)

    if not entries:
        print("No matching binaries found.")
        sys.exit(1)

    print(f"Loading results for {len(entries)} binaries across all metrics...")
    df = _load_all_results(entries)

    if df.empty:
        print("No results found.")
        sys.exit(1)

    print(f"Loaded {len(df)} function records.")

    # Create output directory
    STATS_DIR.mkdir(parents=True, exist_ok=True)

    # Write text summary
    _write_summary(df, STATS_DIR / "summary.txt")

    # Generate plots
    _setup_style()
    print("Generating plots...")
    _plot_variant_scores(df, STATS_DIR / "variant_scores.png")
    _plot_score_distributions(df, STATS_DIR / "score_distributions.png")
    _plot_ast_vs_score(df, SIZE_BINS, "ast_node_count", "AST Node Count",
                       STATS_DIR / "ast_size_vs_score.png")
    _plot_ast_vs_score(df, DEPTH_BINS, "ast_depth", "AST Depth",
                       STATS_DIR / "ast_depth_vs_score.png")
    _plot_metric_correlation(df, STATS_DIR / "metric_correlation.png")
    _plot_source_len_vs_score(df, STATS_DIR / "source_len_vs_score.png")
    _plot_score_cdfs(df, STATS_DIR / "score_cdfs.png")
    _plot_codebleu_submetrics(df, STATS_DIR / "codebleu_submetrics.png")
    _plot_paired_diffs(df, STATS_DIR / "paired_diffs.png")
    _plot_variant_scores_weighted(df, STATS_DIR / "variant_scores_weighted.png")
    _plot_repo_scores(df, STATS_DIR / "repo_scores.png")

    print("\nDone. All output in statistics/")


if __name__ == "__main__":
    main()
