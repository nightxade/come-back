# Paper Outline: LLM-Enhanced Go Binary Decompilation

## 1. Introduction
- Binary decompilation recovers source-level code from compiled executables; critical for reverse engineering, security auditing, and malware analysis
- Traditional decompilers (Ghidra, IDA) produce C-like pseudocode that is structurally and semantically distant from the original source, especially for non-C languages
- Go presents unique challenges: static linking produces large binaries (100MB+), unconventional calling conventions, goroutines, interface dispatch, and compiler-generated artifacts (closures, defer wrappers, generic instantiations)
- LLMs have shown promise for C decompilation (LLM4Decompile, DeGPT, DecLLM), but **no prior work targets Go** with modern LLMs
- We present an end-to-end pipeline that scrapes real-world Go repositories, compiles them at multiple optimization levels, decompiles with Ghidra, and uses Google Gemini to recover idiomatic Go source -- along with a multi-metric evaluation framework
- Contributions:
  - First large-scale LLM-based decompilation system targeting Go
  - A novel benchmark for Go binary decompilation: real-world Go binaries compiled across three build variants with aligned per-function source ground truth, suitable for future evaluation of Go decompilation techniques
  - A pluggable evaluation framework combining CodeBLEU, LLM-as-judge, and syntax validity metrics with AST-complexity-binned analysis

## 2. Background
- **Traditional decompilation**: Ghidra's decompiler pipeline (disassembly, lifting to P-code IR, decompilation to C pseudocode); limitations with Go binaries (missed functions, verbose output, no type recovery for stripped builds)
- **Go binary structure**: The `pclntab` (program counter line table) survives stripping and enables function boundary/name recovery; module path embedded via `go version -m` enables user-function filtering; build variants (`default`, `debug` with `-gcflags=-N -l`, `stripped` with `-ldflags=-s -w`) affect decompilability
- **GoReSym**: Symbol recovery tool that parses `pclntab` to restore function names before Ghidra analysis; useful even on unstripped binaries where Ghidra misses functions
- **LLMs for code generation**: Gemini as the inference model for recovering Go source from Ghidra pseudocode

## 3. Methodology

### 3.1 Dataset Construction
- Automated scraping of popular Go repositories from GitHub
- Compilation of all `main` packages in three build variants (default, debug, stripped)
- Source mapping via `go list -deps -json` to establish ground truth
- User-function pre-filtering via module path extraction (eliminates 80-99% of stdlib/runtime functions)

### 3.2 Decompilation Pipeline
- Ghidra headless analysis via PyGhidra with GoReSym symbol injection on all variants
- Per-function decompilation with 600s timeout; sidecar metadata tracking success/failure/timeout counts
- Chunking into per-function `.c` files with package-aware directory structure; parallel source chunking with aligned naming for evaluation

### 3.3 LLM Inference
- Google Gemini API with batch processing for throughput
- Per-function prompting: each function's Ghidra decompiled C pseudocode is sent individually, and the LLM recovers the corresponding Go function

### 3.4 Evaluation Framework
- **Pluggable metric architecture**: each metric implements `compare_functions()` and `aggregate()`, registered via module naming convention
- **CodeBLEU**: n-gram, weighted n-gram, AST match, and data-flow match components
- **LLM-as-judge**: Gemini rates semantic similarity on a 0-10 scale; supports batch evaluation
- **Syntax validity**: tree-sitter Go parser checks whether inferred output is syntactically valid Go
- **Source-length weighting**: all metrics report both unweighted and source-length-weighted aggregates so larger functions carry proportional influence
- **AST-complexity binning**: functions are binned by source AST node count and tree depth to analyze how model performance correlates with function complexity

## 4. Results
- Dataset scale: number of repos, binaries, and functions across variants
- **Aggregate scores**: mean and weighted CodeBLEU, LLM-judge, and syntax validity across the dataset
- **Syntax validity**: proportion of inferred functions that are syntactically valid Go
- **Complexity analysis**: relationship between AST complexity and recovery quality
  - AST node count bins: how mean score varies across function size categories
  - AST depth bins: how mean score varies across function depth categories
  - Whether syntax failures concentrate in more complex functions
- **Build variant comparison**: how stripped vs. default vs. debug affects recovery quality; Ghidra function coverage across variants
- **Ghidra limitations**: failure modes on large binaries and debug builds with disabled optimizations

## 5. Related Work
- **LLM-based decompilation**: LLM4Decompile (fine-tuned DeepSeek-Coder on assembly-to-C pairs; EMNLP 2024), SLaDe (small purpose-built transformer; CGO 2024), DeGPT (three-role prompting for readability; NDSS 2024), Nova (hierarchical attention + contrastive learning; ICLR 2025), DecLLM (iterative LLM repair of Ghidra output; ISSTA 2025), D-LiFT (RL fine-tuning with D-Score; 2025)
- **Evaluation metrics**: community shift from BLEU/edit distance (inadequate for code) to re-compilability and re-executability as gold standards; LLM-as-judge gaining traction for readability assessment
- **Datasets and benchmarks**: HumanEval-Decompile (164 synthetic C functions), ExeBench (687K executable C functions), Decompile-Bench (2M real-world pairs; NeurIPS 2025), DecompileBench (23K real-world functions; ACL 2025) -- all C-only; no existing benchmark for Go decompilation
- **Go-specific work**: BTC (NDSS BAR 2023) is the only neural decompilation work to evaluate on Go, and only minimally; GoReSym and traditional RE tooling exist but no LLM-based Go decompilation
- **Stripped binary symbol recovery**: GENNM, SymGen, ReSym address name recovery as a separate problem; our pipeline integrates GoReSym directly
- **Key gap we address**: no prior work applies modern LLMs to Go decompilation at scale, no evaluation framework bins results by source-level complexity, and no Go decompilation benchmark exists

## 6. Limitations

### 6.1 Data Contamination
- Many repos likely appeared in LLM training data, inflating evaluation scores
- Difficult to construct an uncontaminated Go binary corpus outside existing open source repositories
- Future work: quantify the extent of data contamination on success inflation

### 6.2 Context Window Size
- Per-function chunking means the LLM cannot leverage cross-function context (e.g. pointer targets, shared type definitions)
- Example: `wavetermdev/waveterm/server/pkg/wstore/DBDelete.go` references a function pointer not included in its chunk
- Larger context windows could enable whole-binary or whole-package inference

### 6.3 Chunking Strategy
- Function-level chunking was adopted after observing that saturating the API with ~1M token single requests produced nonsensically simple and small outputs, even below the output token limit
- Isolating each function improved output quality, but forecloses cross-function reasoning
- Future work: explore alternative chunking granularities (package-level, call-graph-based)

### 6.4 String Recovery
- Current string recovery is heuristic-based and incomplete
- A Ghidra MCP integration could allow the LLM to query and recover strings itself
- IDA Pro's more advanced plugins would also improve heuristic-based Go string recovery

### 6.5 Compiler Optimizations
- **Inlined functions**: the Go compiler inlines functions for optimization, causing slight deflation of accuracy metrics when the inferred code doesn't match the inlined structure; also slightly reduces the comparable function corpus (e.g. `wavetermdev/waveterm/server/pkg/wcloud/sendTEventsbatch.go` with `MarkTEventsAsUploaded.go`). Also occurs with variables defined as equal to static strings (e.g. `wavetermdev/waveterm/server/main/createMainWshClient.go`)
- **Generic functions**: Go generates a separate function for each concrete type instantiation; with function chunking the LLM cannot recognize the pattern and coalesce type-specific variants into a single generic function

### 6.6 Non-Function Recovery
- The LLM currently recovers only function bodies; structs, imports, constants, and other declarations are not targeted
- Such information is generally supported by cross-function context, which is difficult to provide in a short context window
- Future work: apply coding agents (e.g. Gemini CLI) to recover supplemental information

### 6.7 Library Function Isolation
- Each binary's library functions are decompiled in isolation, simulating a reverse engineer receiving a single binary
- Providing cross-binary library decompilations could improve recovery accuracy
- Decompiling each library function only once (upon first observation) could reduce LLM query costs
- Future work: evaluate the effect of shared library context on recovery quality

### 6.8 Re-executability
- Rebuilding a Go binary requires coalescing functions into packages, resolving imports, and setting up the module structure -- a substantially harder task than per-function recovery
- Validating re-executability requires behavioral testing, which is difficult for repos with limited unit tests and in general hard to achieve full coverage
- A general coding agent embedded in the recovered repo may be needed to automate this

### 6.9 Obfuscation
- The dataset includes no obfuscated binaries (e.g. removed `pclntab`, control flow flattening)
- Many pipeline heuristics -- function name recovery, string recovery, module path extraction -- would fail on obfuscated Go binaries

## 7. Conclusion
- Summary: end-to-end pipeline for Go binary decompilation using Gemini, with a multi-metric evaluation framework and a novel benchmark for future Go decompilation research
- Key findings: relationship between function complexity and recovery quality; source-length weighting reveals differences between unweighted and weighted aggregates
- Future work: per-function compilability via Go syntax scaffolding; comparison across model sizes and prompting strategies; fine-tuning on Go-specific decompilation pairs; larger context windows for cross-function reasoning; coding agents for full binary reconstruction
