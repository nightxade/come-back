# LLM Golang Decompiler

LLM-enhanced decompiler from compiled Go binaries back to source code. The pipeline scrapes popular Go repositories, compiles them at multiple optimization levels, decompiles the binaries with Ghidra, then uses Google's Gemini API to recover the original Go source.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Go, and [Ghidra](https://ghidra-sre.org/) (for decompilation).

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
├── decomps/{owner__repo}/{variant}/{binary}.c  # Ghidra decompilation output
└── decomps_filtered/{owner__repo}/{variant}/{binary}.c  # Filtered (user-only) decomps

out/
└── {owner__repo}/{variant}/{binary}/
    ├── metadata.json       # Inference metadata (mode, model, tokens, per-function status)
    ├── {func_name}.go      # Per-function recovered Go source
    └── whole.go            # Whole-file recovered Go source (--whole-file)
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
```

### 3. Decompile binaries with Ghidra

Runs Ghidra's decompiler on each binary via PyGhidra. Outputs one `.c` file per binary with `// Function: {name}` markers.

```bash
uv run decompile
uv run decompile --repo ollama/ollama --variant default
uv run decompile --max-repos 10 --max-size 200 --threads 4
uv run decompile --force   # re-decompile existing outputs
```

| Flag | Description |
|------|-------------|
| `--repo` | Filter to a specific repo (e.g. `ollama/ollama`) |
| `--variant` | Filter to a build variant (`default`, `debug`, `stripped`) |
| `--max-repos` | Limit number of repos |
| `--max-size` | Skip binaries larger than N MB |
| `--threads` | Parallel worker processes (default: 1) |
| `--force` | Re-decompile even if output exists |

### 4. Filter decomps to user-only functions

Raw Ghidra decomps include all functions (stdlib, runtime, external deps) and are typically too large for LLM context windows (e.g. 24 MB, ~7000 functions for a single binary). This step extracts the Go module path from each binary via `go version -m` and keeps only functions belonging to the user's module, reducing output by 80--99%.

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

The inference step automatically prefers filtered decomps when available.[^1]

### 5. Validate filtering (optional)

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

### 6. LLM inference with Gemini

Sends Ghidra decompilations and/or raw binaries to Gemini to recover Go source code. Uses the Batch API by default for efficiency.

```bash
uv run infer --mode decomp --repo ollama/ollama
uv run infer --mode decomp+binary --max-repos 5
uv run infer --mode binary --variant default
uv run infer --mode decomp --per-function
uv run infer --mode decomp --no-batch --threads 4   # synchronous mode
```

| Flag | Description |
|------|-------------|
| `--mode` | **Required.** `decomp`, `binary`, or `decomp+binary` |
| `--repo` | Filter to a specific repo |
| `--variant` | Filter to a build variant |
| `--max-repos` | Limit number of repos |
| `--max-size` | Skip binaries larger than N MB |
| `--threads` | Parallel threads for sync mode / binary uploads (default: 1) |
| `--force` | Re-run even if output exists |
| `--per-function` | Process per-function instead of whole file |
| `--model` | Gemini model (default: `gemini-2.5-flash-lite`) |
| `--no-batch` | Use synchronous API instead of Batch API |

Modes:
- **decomp** — sends only the Ghidra `.c` decompilation
- **binary** — sends only the raw compiled binary
- **decomp+binary** — sends both together

### Utilities

**Count tokens** for a file using the Gemini API:

```bash
uv run count-tokens data/decomps/ollama__ollama/default/chat.c
uv run count-tokens data/binaries/ollama__ollama/default/chat --model gemini-2.0-flash-lite
```

[^1]: **Stripped binaries.** Filtering currently targets unstripped variants (`default`, `debug`) only. For `stripped` binaries, Ghidra replaces symbol names with addresses (`FUN_XXXXXXXX`), so the module-path filter cannot match them directly. [GoReSym](https://github.com/mandiant/GoReSym) can recover full symbol names and addresses from stripped Go binaries via pclntab parsing, which would allow address-based filtering and renaming. However, Ghidra's decompilation of stripped Go binaries is severely limited — in our testing it recovered only ~55% of functions compared to unstripped builds, and only ~7% of user-defined repo functions appeared in the stripped decomp. The bottleneck is Ghidra's analysis, not symbol recovery.
