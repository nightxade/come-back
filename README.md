# ComeBack: Assessing LLM Decompilation for Go

**ComeBack** is a refinement-based LLM decompilation pipeline that recovers Go source code from compiled binaries. It decompiles Go binaries with Ghidra, applies custom string recovery heuristics, and prompts a large language model (Gemini) to reconstruct the original Go source on a per-function basis. The repository also includes a large-scale benchmark of 128 popular open-source Go projects (1,665 binaries, ~1.8M function-level evaluations) and an evaluation suite measuring CodeBLEU, LLM-as-a-Judge semantic similarity, and syntax validity.

For details, see the accompanying paper in `paper/`.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Go, and [Ghidra](https://ghidra-sre.org/). Optional: [GoReSym](https://github.com/mandiant/GoReSym) for improved symbol recovery from Go's `pclntab`.

```bash
uv sync
cp .env.example .env   # fill in GEMINI_API_KEY
```

Set `GHIDRA_INSTALL_DIR` if Ghidra is not at `/opt/ghidra`.

## Pipeline

All commands are installed as entry points via `pyproject.toml`. Most accept `--repo`, `--variant`, `--max-repos`, and `--force` flags; run any command with `--help` for details.

| Step | Command | Description |
|------|---------|-------------|
| 1 | `uv run scrape-repos` | Discover, clone, and compile popular Go repos in three build variants |
| 2 | `uv run map-sources` | Map each binary to its contributing source files via `go list` |
| 3 | `uv run decompile` | Decompile binaries with Ghidra (via PyGhidra), filtering to user-authored functions |
| 4 | `uv run chunk-decomps` | Split decompilations into per-function `.c` files |
| 5 | `uv run chunk-sources` | Split ground-truth Go source into per-function `.go` files |
| 6 | `uv run infer` | Send per-function decompilations to Gemini for Go source recovery |
| 7 | `uv run compare --metric <name>` | Evaluate recovered code against ground truth |
| 8 | `uv run statistics` | Aggregate results and generate summary statistics and plots |

Build variants: `default`, `debug` (`-gcflags=-N -l`), `stripped` (`-ldflags=-s -w`).

### Evaluation metrics

| Metric | Command | Description |
|--------|---------|-------------|
| `codebleu` | `uv run compare --metric codebleu` | Lexical + structural similarity (local, no API key) |
| `llm` | `uv run compare --metric llm` | LLM-as-a-Judge semantic similarity via Gemini |
| `syntax` | `uv run compare --metric syntax` | Syntax validity via tree-sitter (local) |

Custom metrics can be added by creating a module at `src/proj261/eval/comparisons/<name>.py`; see existing metrics for the interface.

## Directory Layout

```
data/
  repos/{owner__repo}/                          # Cloned repositories
  binaries/{owner__repo}/{variant}/{binary}     # Compiled binaries
  decomps_chunked/{owner__repo}/{variant}/{binary}/{pkg}/{func}.c
  source_chunked/{owner__repo}/{variant}/{binary}/{pkg}/{func}.go

out/
  pred/{owner__repo}/{variant}/{binary}/{pkg}/{func}.go   # LLM-recovered source
  results/{metric}/{owner__repo}/{variant}/{binary}.json  # Evaluation results

paper/                                          # Typst source, figures, and bibliography
statistics/                                     # Aggregated results and plots
```

## Citation

```bibtex
@article{cai2025comeback,
  title   = {ComeBack: Assessing LLM Decompilation for Go},
  author  = {Cai, Matthew and Bedouch, Jonah},
  year    = {2026}
}
```
