# LLM Golang Decompiler

LLM-enhanced decompiler from compiled Go binaries back to source code. The pipeline scrapes popular Go repositories, compiles them at multiple optimization levels, decompiles the binaries with Ghidra, then uses Google's Gemini API to recover the original Go source.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Go, and [Ghidra](https://ghidra-sre.org/) (for decompilation). Optional: [GoReSym](https://github.com/mandiant/GoReSym) (for symbol recovery from Go's `pclntab`).

```bash
uv sync
cp .env.example .env   # then fill in your GEMINI_API_KEY
```

Set `GHIDRA_INSTALL_DIR` if Ghidra is not at `/opt/ghidra`:

```bash
export GHIDRA_INSTALL_DIR=/path/to/ghidra
```

## Directory Layout

```
data/
├── metadata.json           # Repo/binary tracking (written by scrape-repos)
├── source_map.json         # Binary→source file mappings (written by map-sources)
├── repos/{owner__repo}/    # Cloned Go repositories
├── binaries/{owner__repo}/{variant}/{binary}   # Compiled binaries
├── decomps/{owner__repo}/{variant}/
│   ├── {binary}.c                              # Ghidra decompilation output
│   ├── {binary}.meta.json                      # Decompilation metadata sidecar
│   └── {binary}.partial                        # Marker for incomplete decompilations (removed on success)
├── decomps_filtered/{owner__repo}/{variant}/{binary}.c  # Filtered (user-only) decomps
├── decomps_chunked/{owner__repo}/{variant}/{binary}/    # Per-function decomp files
│   ├── {package}/{function}.c
│   └── manifest.json
└── source_chunked/{owner__repo}/{variant}/{binary}/     # Per-function source files
    ├── {package}/{function}.go
    └── manifest.json

out/
├── pred/
│   ├── pending_batches.json    # Tracks submitted batch jobs awaiting retrieval
│   └── {owner__repo}/{variant}/{binary}/
│       ├── metadata.json       # Inference metadata (mode, model, tokens, per-function status)
│       └── {package}/{function}.go  # Per-function recovered Go source
└── results/{metric}/{owner__repo}/{variant}/{binary}.json  # Evaluation results
```

Build variants: `default`, `debug` (`-gcflags=-N -l`), `stripped` (`-ldflags=-s -w`).

## Pipeline

All commands are installed as entry points via `pyproject.toml`:

### 1. Scrape and compile Go repositories

Discovers popular Go repos on GitHub, clones them, and compiles all main packages in three build variants.

```bash
uv run scrape-repos
uv run scrape-repos --max-repos 50
uv run scrape-repos --discover-only    # only discover, don't clone/compile
uv run scrape-repos --compile-only     # only compile already-cloned repos
```

### 2. Map binaries to source files

Uses `go list -deps -json` to record which source files end up in each binary.

```bash
uv run map-sources
uv run map-sources --repo ollama/ollama
uv run map-sources --repo ollama/ollama --force   # regenerate mappings for this repo
```

| Flag | Description |
|------|-------------|
| `--repo` | Filter to a specific repo |
| `--force` | Drop existing entries for specified repos (or all) and regenerate |

### 3. Decompile binaries with Ghidra

Runs Ghidra's decompiler on each binary via PyGhidra. Outputs one `.c` file per binary with `// Function: {name}` markers. [GoReSym](https://github.com/mandiant/GoReSym) is automatically invoked (if installed) on **all variants** to recover function names and boundaries from Go's `pclntab` before Ghidra's auto-analysis — Ghidra sometimes misses user functions that GoReSym recovers, even in unstripped binaries.[^1]

Before decompiling, the module path is extracted from each binary via `go version -m`. Only functions belonging to the user's module (or `main.*`) are decompiled — stdlib, runtime, and external dependency functions are skipped. This eliminates 80–99% of decompilation work and avoids Ghidra decompiler errors on complex runtime functions.

Ghidra analysis runs without a timeout (so large binaries complete fully). Individual functions get a 600-second decompilation timeout; functions that exceed this are skipped and logged. The decompiler distinguishes real timeouts from instant Ghidra failures (e.g. locked varnode errors).

A sidecar `<binary>.meta.json` is written alongside each `.c` file with decompilation statistics: `total_functions`, `decompiled`, `skipped`, `timed_out`, `errors`, and `module_path`. A `.partial` marker file tracks incomplete decompilations — without `--force`, the skip logic checks for `.meta.json` (new-format complete) or `.c` without `.partial` (legacy complete), and re-runs interrupted decomps.

```bash
uv run decompile
uv run decompile --repo ollama/ollama --variant default
uv run decompile --repo ollama/ollama --variant default stripped
uv run decompile --repo ollama/ollama --binaries chat ollama
uv run decompile --max-repos 10 --max-size 200 --threads 4
uv run decompile --force   # re-decompile existing outputs
```

| Flag | Description |
|------|-------------|
| `--repo` | Filter to specific repo(s) (e.g. `ollama/ollama`) |
| `--variant` | Filter to build variant(s); accepts multiple values (e.g. `default stripped`) |
| `--binaries` | Decompile specific binary names only (requires exactly one `--repo`) |
| `--max-repos` | Limit number of repos |
| `--max-size` | Skip binaries larger than N MB |
| `--threads` | Parallel worker processes (default: 1) |
| `--force` | Re-decompile even if output exists |

### 4. Filter decomps to user-only functions

**Note:** New decompilations (with `.meta.json` sidecars) are already pre-filtered to user functions at decompile time. This step is only needed for legacy decomps that contain all functions, or as a standalone re-filter tool.

Extracts the Go module path from each binary via `go version -m` and keeps only functions belonging to the user's module, reducing output by 80–99%.

```bash
uv run filter-decomps
uv run filter-decomps --repo ollama/ollama
uv run filter-decomps --variant default --force
uv run filter-decomps --max-repos 10
```

| Flag | Description |
|------|-------------|
| `--repo` | Filter to a specific repo |
| `--variant` | Filter to a build variant |
| `--max-repos` | Limit number of repos |
| `--force` | Re-filter even if output exists |

The inference step automatically prefers chunked decomps when available, then filtered, then raw.[^1]

### 5. Chunk decomps into per-function files

Splits each filtered decomp into one file per function, organized into package subdirectories that mirror the Go source layout (the module path prefix is stripped using `go version -m`). Each function's decompiled C pseudocode gets its own `.c` file, enabling per-function LLM inference. Go generic instantiation shapes (e.g. `Func[go.shape.struct_{...}]`) are simplified to a short type tag in the filename (e.g. `Func_struct.c`); duplicates from multiple instantiations of the same kind get numeric suffixes. Each manifest entry includes a `source_function` field that maps compiler-generated artefacts (closures like `Foo.func1`, defer wrappers like `Foo.deferwrap1`, generic instantiations) back to the source-level declaration, enabling grouping for evaluation.

```bash
uv run chunk-decomps
uv run chunk-decomps --repo hashicorp/terraform
uv run chunk-decomps --force
```

| Flag | Description |
|------|-------------|
| `--repo` | Filter to a specific repo |
| `--variant` | Filter to a build variant |
| `--max-repos` | Limit number of repos |
| `--force` | Re-chunk even if output exists |

The inference step automatically prefers chunked decomps when available.

### 6. Chunk source files for evaluation

Parses Go source files from `data/repos/` into per-function `.go` files using the same naming pipeline as `chunk-decomps`, so output paths align for evaluation (CodeBLEU, edit distance, compilability). Reads `source_map.json` to determine which source files belong to each binary.

```bash
uv run chunk-sources
uv run chunk-sources --repo ollama/ollama
uv run chunk-sources --variant default --force
uv run chunk-sources --max-repos 10
```

| Flag | Description |
|------|-------------|
| `--repo` | Filter to a specific repo |
| `--variant` | Filter to a build variant |
| `--max-repos` | Limit number of repos |
| `--force` | Re-chunk even if output exists |

### 7. Validate filtering (optional)

Compares the filter's classification against `source_map.json` ground truth at the package level. With `--deep`, also verifies at the function level by parsing Go source declarations and tracing them through the binary's symbol table into the decomp.

```bash
uv run validate-filter
uv run validate-filter --repo hashicorp/terraform --deep
uv run validate-filter --variant default
```

| Flag | Description |
|------|-------------|
| `--repo` | Validate a specific repo only |
| `--variant` | Validate a specific variant only |
| `--deep` | Also validate at the function level (requires `data/repos/`) |

Deep validation distinguishes between filter bugs (function is in the raw decomp but missing from the filtered one) and Ghidra limitations (function is in the binary but Ghidra failed to decompile it).

False positives (filter includes a function not in `source_map.json`) are informational — they happen when the source map has incomplete package coverage, which is expected. Only false negatives (filter misses a real user function) are treated as failures.

### 8. LLM inference with Gemini

Sends Ghidra decompilations and/or raw binaries to Gemini to recover Go source code. Uses the Batch API by default in a fire-and-forget pattern: `submit_batch_inference` uploads work and records the job in `pending_batches.json`, and `retrieve_batch_results` polls for completed jobs and downloads results. This allows submitting large batch jobs and retrieving results later.

```bash
uv run infer --mode decomp --repo ollama/ollama
uv run infer --mode decomp+binary --max-repos 5
uv run infer --mode binary --variant default
uv run infer --mode decomp --max-calls 100
uv run infer --mode decomp --no-batch --threads 4   # synchronous mode
uv run infer --retrieve                              # check pending jobs and download results
```

| Flag | Description |
|------|-------------|
| `--mode` | **Required** (except with `--retrieve`). `decomp`, `binary`, or `decomp+binary` |
| `--repo` | Filter to a specific repo |
| `--variant` | Filter to a build variant |
| `--max-repos` | Limit number of repos |
| `--max-binaries` | Limit total number of binaries to process |
| `--max-calls` | Limit total number of LLM inference calls |
| `--threads` | Parallel threads for sync mode / binary uploads (default: 1) |
| `--force` | Re-run even if output exists |
| `--model` | Gemini model (default: `gemini-3.1-flash-lite`) |
| `--no-batch` | Use synchronous API instead of Batch API |
| `--retrieve` | Check pending batch jobs and download completed results |

Modes:
- **decomp** — sends only the Ghidra `.c` decompilation
- **binary** — sends only the raw compiled binary
- **decomp+binary** — sends both together

### 9. Evaluate inference output

Compares recovered `.go` files against the original source chunks using pluggable comparison metrics. Matches functions by file stem (the shared naming pipeline from `chunk-decomps` / `chunk-sources`), calls a metric's `compare_functions`, and writes per-binary JSON results to `out/results/`.

```bash
uv run compare --metric example
uv run compare --metric example --repo ollama/ollama --variant default
uv run compare --metric example --max-repos 5
uv run compare --metric example --force   # re-evaluate existing results
```

| Flag | Description |
|------|-------------|
| `--metric` | **Required.** Name of comparison metric (loads `proj261.eval.comparisons.<metric>`) |
| `--repo` | Filter to a specific repo |
| `--variant` | Filter to a build variant |
| `--max-repos` | Limit number of repos |
| `--max-binaries` | Limit total number of binaries to process |
| `--force` | Re-evaluate even if results exist |

**Writing a custom metric.** Create a module at `src/proj261/eval/comparisons/<name>.py` that defines:

```python
def compare_functions(source: str, inferred: str, decomp: str, metadata: dict) -> dict:
    """Return at least {"score": float_0_to_1}."""

def aggregate(results: list[dict]) -> dict:
    """Summarize a list of per-function result dicts."""
```

Then run it with `--metric <name>`. Metrics may also define optional `add_args(parser)` and `configure(args)` hooks to register and consume their own CLI flags.

#### Built-in metrics

**`codebleu`** — [CodeBLEU](https://github.com/k4black/codebleu): weighted combination of n-gram match, weighted n-gram match, AST match, and data-flow match. Runs locally, no API key needed.

```bash
uv run compare --metric codebleu --repo ollama/ollama
```

Per-function results include `score`, `ngram_match`, `weighted_ngram_match`, `syntax_match`, `dataflow_match`, and `source_len` (character count of the original Go source). Aggregates report both unweighted means (`mean_score`, etc.) and source-length-weighted means (`weighted_score`, etc.), so larger/more complex functions have proportionally more influence on the weighted aggregate.

**`llm`** — LLM-as-a-judge via the Gemini API. Asks the model to rate semantic similarity between original and inferred code on a 0–10 scale (normalized to 0–1). Requires `GEMINI_API_KEY`. Aggregates report both `mean_score` and `weighted_score` (weighted by source length); error results are excluded from both.

```bash
uv run compare --metric llm --repo ollama/ollama
uv run compare --metric llm --explain              # include per-function explanations
uv run compare --metric llm --model gemini-2.5-flash  # use a different judge model
```

| LLM-specific flag | Description |
|-------------------|-------------|
| `--explain` | Ask the model for an explanation alongside the score |
| `--model` | Gemini model to use as judge (default: `gemini-3.1-flash-lite-preview`) |

**`syntax`** — Syntax validity check via tree-sitter. Parses each inferred Go function and scores 1.0 for a clean parse, 0.0 if any syntax errors are found. Runs locally, no API key needed.

```bash
uv run compare --metric syntax --repo ollama/ollama
```

Per-function results include `score`, `valid` (boolean), `error_count` (number of ERROR/MISSING AST nodes), `node_count` (total AST nodes in the inferred output), and `source_len`. Aggregates report `mean_score`, `weighted_score`, and `valid_count` / `invalid_count` / `total`.

**`example`** — Placeholder that always returns 0. Useful for testing the framework end-to-end.

#### AST-binned statistics

Breaks down evaluation scores by Go source AST complexity (node count and tree depth) to show how the model performs on simple vs. complex functions. Reads existing result JSONs and parses source chunks with tree-sitter.

```bash
uv run eval-ast --metric llm
uv run eval-ast --metric codebleu --repo ollama/ollama --variant default
uv run eval-ast --metric llm --max-repos 5
```

| Flag | Description |
|------|-------------|
| `--metric` | **Required.** Name of comparison metric to analyze |
| `--repo` | Filter to specific repo(s) |
| `--variant` | Filter to a build variant |
| `--max-repos` | Limit number of repos |
| `--max-binaries` | Limit total number of binaries |

### Utilities

**Count tokens** for a file using the Gemini API:

```bash
uv run count-tokens data/decomps/ollama__ollama/default/chat.c
uv run count-tokens data/binaries/ollama__ollama/default/chat --model gemini-2.0-flash-lite
```

[^1]: **GoReSym symbol recovery.** GoReSym now runs on **all variants** (default, debug, stripped), not just stripped binaries. Go's `pclntab` (program counter line table) survives even in stripped binaries because the runtime needs it. GoReSym parses `pclntab` to recover function names and boundaries, which are injected into Ghidra before auto-analysis. This restores real Go symbol names (e.g. `github.com/ollama/ollama/api.Func` instead of `FUN_004a1000`), enabling the downstream filter and chunk pipeline to classify them by module path. Running GoReSym on unstripped binaries is also valuable because Ghidra sometimes misses user functions that GoReSym recovers from `pclntab`. If GoReSym is not installed, the decompiler degrades gracefully and continues with Ghidra's default `FUN_` names. Note: Ghidra's decompilation of stripped Go binaries is still limited — in our testing it produced output for only ~55% of functions compared to unstripped builds, even with correct boundaries from GoReSym. The bottleneck is Ghidra's decompiler (lacking type info), not symbol recovery.
