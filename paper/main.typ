#import "@preview/charged-ieee:0.1.4": ieee
#import "assets/pipeline.typ": pipeline-diagram
#import "assets/string-recovery.typ": string-recovery

#show: ieee.with(
  title: [
    ComeBack: Assessing LLM Decompilation for Go
  ],
  abstract: [
    Recent advancements in the quality of state-of-the-art (SOTA) LLMs have yielded promising results across a myriad of fields. In response to this, recent works have attempted to improve the quality of decompilation results by allowing LLMs to refine classical decompilations provided by Ghidra. However, this technique has been almost exclusively explored in C, not in high-level programming languages, which pose many unique challenges to traditional decompilation, including large, statically linked standard libraries and language runtimes. Go is a particularly important target: it dominates cloud-native infrastructure and has seen rapid adoption among malware authors, making reverse engineering Go binaries an increasingly critical task.

    This paper provides two main contributions. First, we assemble a benchmark consisting of the source code, binaries, and Ghidra decompilations of popular Go projects. Next, we implement a system that prompts a SOTA model to recover the original Golang based on the Ghidra decompilations, and evaluate the quality of the recovered output using CodeBLEU, LLM-as-a-Judge, and syntax validity metrics.
  ],
  authors: (
    (
      name: "Matthew Cai",
      organization: [UC Berkeley],
      email: "matthew_cai@berkeley.edu",
    ),
    (
      name: "Jonah Bedouch",
      organization: [UC Berkeley],
      email: "jonahbedouch@berkeley.edu"
    )
  ),
  bibliography: bibliography("refs.bib"),
  figure-supplement: [Fig.],
)

= Introduction <sec:introduction>
_Decompilation_ is the process of converting a low-level binary (consisting of assembly instructions) into a higher-level intermediary language, in order to enable analysis of the underlying binary without possessing the source. Decompilation is commonly used in reverse engineering to port or update code whose source has been lost, to better understand the implementation of closed-source programs, and to perform security and malware analysis on programs whose source is not generally available.

Compilation is a lossy, many-to-one transformation: variable names, type annotations, and high-level control structure are discarded, making inversion inherently ambiguous and difficult. Hence, decompilation has been extensively analyzed by both industry and academia in past decades. Classical decompilation techniques rely on complex control-flow analysis, structural analysis, and a large number of hand-specified heuristics in order to convert a binary into an Intermediate Representation (IR), then reconstruct a low-level language, such as C, from this IR @ref:phoenix. While SOTA classical decompilers can analyze primitive and pointer-based types @ref:polytypes, the decompilation process is highly complex, and often produces output that is difficult to analyze for large programs written in C. Additionally, classical decompilers such as Ghidra @ref:ghidra or IDA Pro @ref:idapro often produce significantly degraded decompilations, with regards to readability, for binaries produced by higher-level programming languages that are used for many modern closed-source programs. This is a direct consequence of the presence of large standard libraries, complex dynamic dispatch tables, large pointer-based data structures, unconventional calling conventions, and extensive language runtimes, which add significant complexity and noise to the binary. And, regardless, the decompilation result is ultimately a pseudo-C representation which is challenging to read or analyze, as it is inherently structurally and semantically dissimilar from the source code of the original language.

Due to the above limitations, recent literature has attempted to iterate upon classical decompilation through the use of Large-Language Models (LLMs). Various approaches have been explored, which largely fall into two major techniques. Many systems sidestep the traditional decompilation step entirely, relying on fine-tuned models combined with unique techniques to achieve _end-to-end_ decompilation (@ref:llm4-decompile, @ref:ref-decompile, @ref:idioms, @ref:sk2-decomp, @ref:salt4-decomp, @ref:wadec). Meanwhile, another set of systems rely on existing general-purpose models in order to _refine_ the output of classical decompilers, transforming convoluted, decompiled C code into a more human-readable representation suitable for reverse engineering analysis (@ref:llm4-decompile, @ref:context-guided-decomp, @ref:stack-sight, @ref:augmenting-smart-contract).

In general, however, these LLM-driven techniques focus primarily on decompiling large binaries originally written in C into a more human-readable C than the code output by classical decompilers. Little effort has been placed into exploring the ability of LLMs to lift a binary compiled from a higher-level language back into the syntax of the original language.

Google's Go Programming Language @ref:golang is a particularly interesting target for such an endeavor. Go has become the predominant language for cloud-native infrastructure, with over 75% of projects in the Cloud Native Computing Foundation being written in Go @ref:go-cloud. Moreover, it has grown increasingly popular among malware authors due to its first-class cross-compilation support; its ability to produce statically linked, self-contained binaries without external dependencies; and the simple fact that Go binaries are much more challenging to reverse engineer, particularly when combined with Go obfuscation tools @ref:gobfuscate. In fact, Unit 42 of Palo Alto Networks reported a 2000% increase in identified Go malware samples from 2017 to 2019 @ref:pan-go-malware, while a study by Crowdstrike revealed an 80% increase in Go malware samples in just three months, from June to August 2021 @ref:crowdstrike-go-malware. Consequently, reverse engineers have been increasingly encountering binaries compiled from Go.

This paper serves as an initial exploration into the use of LLM-enhanced decompilation to recover source code written in a fully-featured high-level language, Go. We present _ComeBack_, a refinement-based LLM decompilation suite designed to convert binaries compiled from Go into their original, high-level Go code, by first using Ghidra to produce C-level decompilations, then refining these compilations through the use of a SOTA LLM. Additionally, we construct a benchmark consisting of some of the most used Go programs on GitHub, and evaluate our system against this benchmark, assessing for semantic similarity as our primary measure of success.

The remainder of this paper will be structured as follows. In @sec:related-works, we discuss related works and their applicability to ComeBack. In @sec:methodology, we discuss the process by which we created a dataset in order to audit the effectiveness of ComeBack and our approach for the design and implementation of ComeBack, including the decisions (and constraints) that influenced the final design. In @sec:evaluation, we discuss the results of running ComeBack on our dataset, including qualitative analysis of code readability and quantitative similarity metrics. Finally, we discuss limitations and future directions of exploration in @sec:discussion, before concluding in @sec:conclusion.

= Related Works <sec:related-works>

Much of the existing decompilation literature has focused on enhancing classical and LLM-based methods for decompiling programs originally compiled in C. In this section, we discuss some noteworthy standouts in the realm of LLM-based C decompilation, a few interesting works that discuss decompiling languages outside of C, and seminal decompilation benchmarks. These serve to contextualize our work in Golang decompilation.

We acknowledge the existence of a substantial corpus of work around the decompilation of Java (such as @ref:java-quirks), but choose to omit detailed discussion around this because decompiling Java presents a fundamentally different problem than other programming languages. Java Bytecode is specifically optimized to run in the Java Virtual Machine (JVM), and thus retains a significantly greater portion of its original semantics (including type information). This enables comparatively effective recovery (i.e. 78% semantically equivalent code output relying solely on classical methods in @ref:java-quirks).

== LLM-Based C Decompilation Techniques

LLM4Decompile @ref:llm4-decompile introduced the first open-source LLM series fine-tuned for end-to-end decompilation, known as LLM4Decompile-End, which translates assembly code directly to C. ReF Decompile @ref:ref-decompile proposed augmentations to the end-to-end pipeline: relabeling jump targets with labels to improve control flow recovery and separately inferring type information. SALT4Decompile @ref:salt4-decomp constructs source-level abstract logic trees (SALTs) from binary to approximate higher-level control flow, and fine-tuned an LLM on these representations. SK2Decompile @ref:sk2-decomp decomposes the problem into two sequential phases, structure recovery and identifier naming, and fine-tunes a distinct model for each via reinforcement learning, enabling independent optimization of the control flow correctness and code readability.

Meanwhile, a parallel line of work instead refines the pseudo-C output of classical decompilers. LLM4Decompile also offers a refinement LLM series, LLM4Decompile-Ref, that takes Ghidra's output and returns improved C code. Context-Guided Decompilation @ref:context-guided-decomp leveraged in-context learning @ref:in-context-learning to help guide off-the-shelf LLMs in producing re-executable code from traditional decompilations. Idioms @ref:idioms addressed the lack of focus on recovering user-defined composite type definitions, fine-tuning an LLM in a refinement-based approach to encourage generating appropriate user-defined types alongside decompilations. DeGPT @ref:decgpt deviated from the one-shot LLM paradigm and proposed a multi-agent framework with three distinct, cooperating roles: a referee that proposes an optimization plan to improve the decompilation, an advisor that suggests concrete changes based on the plan, and an operator that verifies the preservation of function semantics. DecLLM @ref:dec-llm similarly departed the one-shot paradigm, iteratively prompting off-the-shelf LLMs to repair decompiler output by using compiler error messages and dynamic runtime feedback as verifiers and LLM context, targeting re-compilability as the primary metric of success.

== Reverse Engineering High-Level Languages

There has been some, though limited, existing work on reverse engineering binaries produced by high-level languages like Rust @ref:decompiling-rust @ref:rust-binary-analysis and Swift @ref:reversing-swift @ref:swift-rev. The reverse engineering community, however, has produced by far the largest collection of tools and techniques for Go, compared to other modern compiled languages, largely due to its growing presence in cloud and malware programs. Here, we briefly overview relevant Go compilation details, as well as the SOTA in reverse engineering Go.

The Go compiler embeds rich metadata in its binaries, most notably the `pclntab` (program counter line table). The `pclntab` maps addresses to function names and source file information---this persists even in stripped binaries that lack a `.symtab` symbol table as it is required by the Go runtime. For context, a stripped C/C++ binary would not contain any function names at all. A `moduledata` structure describes the layout of the executable to support runtime capabilities like garbage collection and reflection, and since Go 1.18, a `buildinfo` table describes compiler flags, target platform, dependency versions, and other relevant build-time metadata. @ref:reversing-go

These structures have enabled the development of several Go reverse engineering tools. GoReSym @ref:goresym parses `pclntab` and `moduledata` to recover function names, type information, and compiler metadata from binaries. The Go Reverse Engineering Tool Kit @ref:goretk provides a Go library (GoRE) and a standalone analyzer (Redress) for extracting similar metadata. Plugins also exist for all major disassemblers: AlphaGolang @ref:alphagolang and IDAGolangHelper @ref:idagolanghelper for IDA Pro @ref:idapro and several similar plugins for Ghidra @ref:ghidra and Binary Ninja @ref:binary-ninja. The recently released IDA Pro 9.2 (September 2025) even possesses first-class Go decompilation support @ref:ida-9.2-golang. There also exists a limited selection of academic works that discuss reverse engineering Go binaries @ref:program-semantics-go. Notably, none of these tools and techniques are designed to recover the original Go source code from the binary, and instead merely intend to assist a reverse engineer's understanding of the binary or improve the quality of decompiled code.

To our knowledge, there is only one prior work that leveraged LLMs to recover the source code of binaries produced by high-level languages. Beyond The C @ref:btc designed and trained a fine-tuned transformer model in order to allow assembly to be lifted into arbitrary languages, including OCaml, Rust, and Go. Their work involves training custom high and low-level tokenization models for each language they wish to output in, and then using a fine-tuned transformer model to attempt reconstruction of original binaries based on decompiled outputs. This work is somewhat similar to what we attempt; however, we rely instead on off-the-shelf decompilation, replace the fine-tuned transformer model with a SOTA LLM (Gemini 3.1 Flash-Lite), and design a refinement-based approach, rather than applying the LLM directly to the binary for end-to-end decompilation.

== Decompilation Benchmarks

Existing benchmarks for evaluating decompiler output focus almost exclusively on C programs targeting x86-64 architecture. First, note that, beyond readability and semantic similarity, _re-compilability_ and _re-executability_ are common metrics of evaluation in past work. Re-compilability describes whether or not the LLM-generated code can be compiled into an actual binary, while re-executability describes whether or not the LLM-generated code behaves like the original source code.

HumanEval-Decompile @ref:llm4-decompile is composed of 164 C-transpilations of the small, individual functions from OpenAI's HumanEval code-generation benchmark @ref:humaneval, and it scores decompiled output primarily by testing if it recompiles under GCC and passes the original assertions. ExeBench @ref:exebench is a large-scale dataset of 4.5 million compilable (700,000 executable) real-world C functions sampled from GitHub repositories, each accompanied by automatically generated IO examples. It was originally designed for usage in ML for program optimization and compilers, but decompilation works such as LLM4Decompile have adopted it as an evaluation benchmark for re-executability. DecompileBench @ref:decompbench comprises 23,400 functions sampled from 130 real-world programs sourced via OSS-Fuzz, and evaluates decompilers along three axes: re-compilability, behavioral correctness via fuzzing-based coverage equivalence, and readability via LLM-as-a-Judge. Finally, Decompile-Bench @ref:decomp-bench provides a large training corpus of 2 million function pairs sampled from GitHub repositories and a separate evaluation dataset consisting of only code published after 2025 to minimize data leakage; it evaluates re-executability, edit similarity, and readability.

Beyond The C @ref:btc also constructed a dataset of Go programs for evaluation. However, their dataset consists largely of interview-style coding problems alongside a few other small repositories and, to our knowledge, they did not publicly release their dataset. In contrast, our benchmark is composed entirely of real-world, actively maintained projects to more accurately capture the types of binaries reverse engineers may encounter. Additionally, we have published our dataset on HuggingFace for public use.

Like previous works, we evaluate semantic similarity via methods such as CodeBLEU and LLM-as-a-Judge. However, we do _not_ measure re-compilability or re-executability metrics, due to limitations inherent to Go and our design choices; we detail our rationale further in @sec:discussion:re. Instead, we compromise by measuring the syntactical validity of the decompilations as a rough approximation of re-compilability.

= Methodology <sec:methodology>

#figure(
  scale(75%, reflow: true)[#pipeline-diagram],
  caption: [Diagram of End-to-End Pipeline],
  placement: auto,
) <fig:pipeline>

// #figure(
//   image("assets/pipeline_diagram.png"),
//   caption: [Diagram of End-to-End Pipeline],
//   placement: auto,
// ) <fig:pipeline>

== Repository Selection and Compilation

We construct a large-scale benchmark for evaluating Go binary decompilation by scraping the most popular open-source Go repositories hosted on GitHub. Using the GitHub Search API @ref:githubapi, we query for non-archived, non-fork repositories with the Go language tag and at least 500 stars, sorted by star count. We clone the top 200, which provides a corpus of mature projects sufficiently diverse in size and domain. For each repository, we identify all `main` packages and compile each into a statically linked `linux/amd64` binary. We also build three variants per binary to evaluate how compilation flags affect the decompilation quality.

- *default*: Default settings, no flags\ (`go build`)
- *debug*: Disables optimizations and inlining\ (`-gcflags="-N -l"`)
- *stripped*: Debug symbols and DWARF info removed\ (`-ldflags="-s -w"`)

This yields \~1,800 binaries per variant.

== Decompilation

We utilize Ghidra's @ref:ghidra headless decompiler via PyGhidra to decompile each binary. Prior to Ghidra's standard analysis, we run GoReSym @ref:goresym: a Go reverse engineering tool that, among other functionalities, recovers function symbol names and locations by parsing the `pclntab`. This is run on all binaries, not just stripped binaries, since GoReSym reliably recovers function names and locations on all tested binaries; in contrast, Ghidra's more limited Go analysis support fails for some functions.

Each Go binary includes all necessary functions for execution: runtime-related, standard library, external library, and user-defined functions. But, as these are duplicated many times across different binaries, analyzing every single appearance of common runtime or library functions would result in these functions dominating the results. Moreover, these functions are of limited decompilation interest---the Go runtime and Go standard library are open-source and well-analyzed, and tools like IDA's Lumina @ref:lumina also allow teams to store common function metadata for recognition across binaries. Thus, we limit our scope to only user-authored code, i.e. code owned by the target repository.

For this purpose, we extract the module path from each binary with `go version -m` and decompile only functions whose name matches `main.*` or whose name prefix matches the module path, which is a link to the Github repo such as `github.com/antonmedv/fx`. The first filters for functions in the main package of the binary, while the second filters for function names like `github_com_antonmedv_fx_internal_toml_toJ`, which are functions defined in non-main packages. (In this instance, this function `toJ` is defined in the package `toml` in the file `internal/toml/toml.go`) This reduces the size of the subset of the binary that is decompiled by 80--99%, substantially reducing computation time and evaluation noise.

Some extremely large functions take very long to decompile, as the time to decompile grows superlinearly with respect to the function size; thus, we add a 10-minute timeout for each function. Only [UPDATE LATER]% of the total corpus of functions are filtered out by this timeout.

== String Recovery

#figure(
  string-recovery,
  caption: [Examples of string recovery heuristics],
  placement: auto,
) <fig:string_recovery_ex>

 In Go binaries, strings are not null-terminated, and are instead placed in the binary in order of length and concatenated together into much larger _packed string blocks_ @ref:gostrings. Then, in the program itself, strings are internally represented as 16-byte `(pointer, length)` pairs, where `pointer` points to somewhere within the larger strings in the `.rodata` of the binary. The Go runtime then extracts the desired string from the packed blocks when necessary. Ghidra, however, expects strings as null-terminated C strings, and does not typically include the strings within the function decompilation itself (occasionally, it does include a portion of the packed blocks after resolving a pointer, which may contain as a substring the desired string). Instead, Ghidra's auto-analysis typically labels these string structures with symbols such as `s_*`, `PTR_s_*`, and `DAT_*`. Without access to the binary itself, it's necessary to resolve the string literals themselves; otherwise, the LLM would see the strings as merely opaque memory references. Thus, ComeBack utilizes some manually-designed heuristics to recover some of the original strings.

 + *Defined Data Scan*: We first scan the data defined by Ghidra for two categories of labels.
  + A symbol named `s_*` points directly to the start of the string in the packed block. We can use Ghidra's data API to directly retrieve the bytes at this location for up to some maximum number of bytes, since we lack the length metadata of the string. Empirically, we use a maximum of 200 bytes; this read may overflow into adjacent strings, but it usually retains the desired string without introducing too much other noise.
  + A symbol named `PTR_*` _may_ represent a pointer to a pointer to a string literal. In other words, the quadword at address `PTR_*` may point to the start of the string in a packed block. Thus, we attempt to dereference `PTR_*`, and then apply the same strategy as used for `s_*` symbols.
 + *Undefined Data Resolution*: The decompiler may also include `PTR_s_*_<hexstr>` and `s_*_<hexstr>` symbols in the pseudocode that are not defined as data items in the Ghidra API listing. We search for such references, parse the hex address suffix, and resolve the memory address---we attempt to read an array of string structs that commonly appear as a result of string tables and slice initializers. For each potential string struct `(pointer, length)`, we read exactly `length` bytes from `pointer`. If array reading fails, we fallback to the simple string resolution described in 1).
 + *DAT Symbols*: For data Ghidra cannot classify, it frequently labels it as `DAT_<hexstr>`. For such symbols, we parse the hex address, interpreting it as the `pointer` to the string, and search the adjacent decompiled C code for a corresponding `length` value. This is because the stack often contains string structs, and they show up in Ghidra decompilations as, e.g., `local_38 = DAT_xxx; local_30 = 0xd;`. We fallback to 1) if this fails.
 + *Hex Literals*: There may also exist raw hex literals in the decompiled code. For such literals, we check if they point to memory within `.rodata`, and then apply the strategy from 3).

Resolved strings are injected as annotations at the top of each function's decompiled output, providing the LLM with the likely string literal (or packed string block) that corresponds to each symbol.

== Function-Level Chunking and Ground Truth

Due to limitations in context window size, detailed further in @sec:discussion, we divide the decompiled output by function, to perform inference on each individually. In order to recover the original ground truth for comparison, we run `go list -deps -json` for each main package in each repository to identify the source code files in the repository that contribute to this package. Then, we parse each file for function and method declarations, and isolate each declaration and corresponding definition into its own file.

Notably, we organize both the chunked decompilations and ground truth code according to the path to the original source code file in the repository. For the ground truth code, this is trivial; for the decompilation, the symbol names provide the path: the recovered source for `github_com_antonmedv_fx_internal_toml_toJ` is placed in `internal/toml/toJ.go`.

Note that, due to compiler-generated artifacts---separate functions for closures and Goroutines (`Foo.func1`), defer wrappers (`Foo.deferwrap1`), and generic instantiations (`Foo[go.shape.struct_{...}]`)---the decompilation chunking process applies some heuristics to consolidate such artifacts with the source-level parent declaration. This is important because the ground truth source includes these details, and the LLM thus needs this context in order to recover these specific sections of code or to contextualize that, e.g., the function is a generic function.

Our benchmark includes these chunked decompilations and ground truth source code.

== LLM-Based Source Recovery

Finally, we frame source recovery as a code translation task: given a single function's Ghidra pseudo-C decompilation, an LLM is prompted to recover the corresponding Go source. We provide the following system instruction:

#block(fill: luma(245), radius: 6pt, inset: 8pt, width: 100%, above: 1em, below: 1em,
  quote(block: true)[You are an expert Go reverse engineer. You will be given a single decompiled C pseudocode function produced by Ghidra from a compiled Go binary. Recover the original Go source code for this function as accurately as possible. Output ONLY valid Go code with no explanation. Exclude package declarations and imports.]
)

And we include the pseudo-C Ghidra decompilation of a single function in the user message.

= Evaluation <sec:evaluation>

== Metrics

We evaluate recovered functions using three complementary metrics that capture different aspects of decompilation quality:

=== CodeBLEU

CodeBLEU @ref:codebleu is a composite evaluation metric, originally designed for assessing code generation quality, that measures four aspects of the code: n-gram precision (lexical similarity), weighted n-gram precision (keyword-aware matching), syntax match (abstract syntax tree's subtree overlap), and dataflow match (variable usage pattern similarity).

=== LLM-as-a-Judge

LLM-as-a-Judge @ref:llm-as-a-judge has grown in popularity as an evaluation metric for tasks where quantitative evaluation is hard. Existing literature on decompilers has increasingly been measuring success with LLM-as-a-Judge, by asking an LLM to assess readability, semantic similarity, or other aspects of the decompiled code by comparing it with the original source code @ref:llm4-decompile @ref:sk2-decomp @ref:ref-decompile.

We employ a separate Gemini instance of the same model (Gemini 3.1 Flash-Lite) to evaluate semantic similarity between the inferred decompilation and the original source code, prompting it to rate semantic similarity on an integer scale from 0 (completely different) to 10 (semantically identical). This serves to capture high-level functional correctness that   lexical metrics may miss; for instance, a function with different variable names or control flow structure than the source code that implements identical logic should score highly on the LLM-as-a-judge semantic similarity metric even if CodeBLEU penalizes the surface differences.

=== Syntax Validity

We parse each decompilation with `tree-sitter` @ref:tree-sitter, a programming language parser, and assign a score of 1 for syntactically valid code and a score of 0 for any syntax errors.

=== Complexity-Weighted Analysis

For all metrics, we report both an unweighted mean and a source-length-weighted mean across the evaluated corpus. Moreover, using `tree-sitter` @ref:tree-sitter, we bin results by:

- AST node count (6 bins: [1,25), [25,50), [50,100), [100,200), [200,500), [500,$infinity$))
- AST depth (6 bins: [1,5), [5,10), [10,15), [15,20), [20,25), [25,$infinity$))

This organization of the results allows us to analyze the relationship between source code complexity and decompilation quality and ensure that the scores of larger, more complex functions are not hidden behind the scores of many small, trivial functions.

== Results

Our final dataset comprises 128 repositories and 1,665 unique binaries, yielding approximately 587,000 (default), 594,000 (debug), and 624,000 (stripped) user-authored functions per variant---over 1.8 million function-level evaluations in total.

=== Aggregate Scores

#figure(
  image("statistics/score_distributions.png", width: 100%),
  caption: [Score distributions by variant across all three metrics. Boxes span Q1--Q3; whiskers extend to 1.5$times$IQR.],
  placement: auto,
) <fig:score-distributions>

@fig:score-distributions summarizes the distribution of all three metrics across build variants. The LLM-as-a-Judge semantic similarity score averages 0.61 for the default and debug variants and 0.55 for stripped, on a 0--10 scale normalized to [0, 1]. CodeBLEU follows a similar pattern, averaging 0.59 (default/debug) and 0.54 (stripped). Syntax validity is near-perfect at 99.6% across all three variants, indicating that the LLM reliably produces parseable Go regardless of build configuration. The source-length-weighted means are substantially lower---0.43 (LLM) and 0.44 (CodeBLEU) for the default variant---reflecting that larger, more complex functions, which contribute more weight, are harder to decompile accurately.

=== Effect of Build Variant

#figure(
  image("statistics/paired_diffs.png", width: 100%),
  caption: [Distribution of paired score differences (default $minus$ stripped) for the same function across build variants.],
  placement: auto,
) <fig:paired-diffs>

To isolate the effect of compilation flags, we perform paired comparisons between the same functions across build   variants. Default and debug builds produce nearly identical scores: the mean paired difference is less than 0.001 for both LLM and CodeBLEU. A Wilcoxon signed-rank test @ref:wilcoxon finds the LLM difference statistically significant ($p = 0.01$) but with a negligible effect size (mean difference $< 0.001$), and no significant difference for CodeBLEU ($p = 0.15$). In contrast, stripping debug symbols and DWARF information degrades both metrics significantly ($p < 10^(-10)$, Wilcoxon). As shown in @fig:paired-diffs, the default variant outperforms stripped by a mean of +0.056 on LLM and +0.045 on CodeBLEU. This is expected: while stripped Go binaries still retain the `pclntab` critical for GoReSym and Ghidra analysis, their lack of symbol-table information still negatively impacts type and variable recovery, forcing the LLM to work with a less informative decompilation. Interestingly, syntax validity is marginally _higher_ for stripped builds (by 0.03 percentage points), though this difference is negligible in practical terms.

=== Complexity Analysis

#figure(
  image("statistics/ast_size_vs_score.png", width: 85%),
  caption: [Mean scores by ground-truth AST node count. LLM and CodeBLEU degrade substantially with complexity; syntax validity remains high.],
  placement: auto,
) <fig:ast-size>

#figure(
  image("statistics/ast_depth_vs_score.png", width: 85%),
  caption: [Mean scores by ground-truth AST tree depth. LLM and CodeBLEU degrade substantially with complexity; syntax validity remains high.],
  placement: auto,
) <fig:ast-depth>

Both semantic similarity metrics exhibit a strong inverse relationship with source code complexity. @fig:ast-size plots mean scores against ground-truth AST node count, while @fig:ast-depth plots mean scores against ground-truth AST tree depth. Functions with 25--50 AST nodes score highest (0.80 LLM, 0.75 CodeBLEU), while functions exceeding 500 nodes score roughly half as well (0.32 LLM, 0.35 CodeBLEU). The relationship with AST depth is similar: functions of depth 5--10 average 0.76 (LLM) and 0.71 (CodeBLEU), declining to 0.27 and 0.29 at depths above 25. This is consistent with the Spearman correlations of $rho = -0.51$ (AST node count vs. LLM) and $rho = -0.50$ (AST node count vs. CodeBLEU). Notably, the smallest functions ([1, 25) nodes) score slightly lower than the [25, 50) bin; these are typically trivial getter/setter or stub functions where the ground truth is a single return statement, and the LLM may over-generate. Syntax validity remains high across all bins, dipping only to 98.4% for functions above 500 nodes.

#figure(
  image("statistics/source_len_vs_score.png", width: 85%),
  caption: [Mean scores by source length (characters). The strong negative trend for LLM and CodeBLEU mirrors the AST-complexity results; syntax validity is largely unaffected.],
  placement: auto,
) <fig:source-len>

@fig:source-len corroborates the AST-complexity findings from a different angle: binning by raw source length (in characters) yields a similarly strong negative trend for both LLM ($rho = -0.51$) and CodeBLEU ($rho = -0.50$), while syntax validity remains nearly flat.

=== CodeBLEU Sub-Metric Breakdown

#figure(
  image("statistics/codebleu_submetrics.png", width: 85%),
  caption: [CodeBLEU sub-metric breakdown by variant. Structural metrics (syntax match, dataflow) score higher than lexical metrics (n-gram, weighted n-gram).],
  placement: auto,
) <fig:codebleu-sub>

Decomposing CodeBLEU into its four sub-metrics (@fig:codebleu-sub) reveals that structural similarity is recovered more effectively than lexical similarity. Dataflow match scores highest (0.73 for default/debug, 0.69 for stripped), followed by syntax match (0.68, 0.64), weighted n-gram (0.46, 0.41), and n-gram (0.42, 0.36). This pattern suggests that the LLM successfully reconstructs the high-level control flow and variable usage patterns of the original Go code, even when exact token sequences differ---likely due to renamed variables, reordered statements, or alternative but semantically equivalent expressions. The n-gram metrics, which penalize such surface-level differences, are correspondingly lower.

=== Metric Agreement

#figure(
  image("statistics/metric_correlation.png", width: 85%),
  caption: [Joint distribution of LLM-as-a-Judge and CodeBLEU scores across all evaluated functions. The strong positive correlation (Pearson $r = 0.77$) indicates that both metrics capture similar underlying quality.],
  placement: auto,
) <fig:metric-corr>

The LLM-as-a-Judge and CodeBLEU metrics are strongly correlated (Pearson $r = 0.77$, Spearman $rho = 0.76$; @fig:metric-corr), lending confidence that the LLM-as-a-Judge scores accurately capture semantic similarity. Both metrics also show moderate negative correlation with source length (Spearman $rho approx -0.50$), confirming the complexity trend observed in the binned analysis.

=== Per-Repository Variation

#figure(
  image("statistics/repo_scores.png", width: 100%),
  caption: [Mean LLM scores for the top and bottom 15 repositories (minimum 20 functions). Substantial inter-repository variation exists beyond what variant or complexity alone explains.],
  placement: auto,
) <fig:repo-scores>

Aggregate statistics mask considerable variation across repositories. @fig:repo-scores shows the top and bottom 15 repositories ranked by mean LLM score. The best-performing repositories achieve mean scores above 0.8, while the worst fall below 0.4---a spread far larger than the variant effect (+0.056). This variation likely reflects differences in coding style, function granularity, and the extent to which idiomatic Go patterns are preserved through compilation and decompilation.


= Discussion <sec:discussion>

== Dataset Contamination

Practically all SOTA LLMs are trained on mass amounts of data scraped from the internet. Considering the recent focus on the application of LLMs to coding tasks @ref:claude-code @ref:codex @ref:geminicli, it is likely that a significant proportion of the most popular Go repositories on GitHub, which comprise our dataset, were seen by LLMs during training. This contamination may induce some systematic error in our evaluations; unfortunately, due to limitations in time and resources, it is difficult to recover a large corpus of Golang binaries and corresponding sources outside of existing open source repositories to create an uncontaminated dataset. We leave it to future work to determine the extent to which data contamination inflates our metrics of success.

== Context Window Size

The Gemini API offers inference with models possessing context windows of up to \~1M tokens. However, the binaries produced from Go programs are large, due to extensive static linking of the Go standard library and external packages---after tokenization, they frequently exceeded the context window limits by 1-2 orders of magnitude. This precluded us from designing or evaluating an end-to-end decompilation pipeline for the programs. Moreover, the decompilations produced by Ghidra are even larger than the binaries themselves; hence, we chose to divide each decompilation into smaller chunks that fit within the context window limit, and perform inference separately. Unfortunately, this restricts the LLM from considering information across the entire decompilation, some of which, e.g. parent/child functions in call graph or methods of the same object, may be critical for accurate reconstruction.

We also found that saturating the context window with requests between 100,000 and 1M input tokens frequently resulted in nonsensically simple outputs with size well below the output token limit. Empirically, isolating each function by itself in the context window allowed the LLM to focus on just the function and produce more coherent outputs; yet, as aforementioned, this restricts the LLM's source of information to merely the decompilation of the target function. This guided our decision to limit the Go source recovery to only functions. While function and control flow recovery is arguably the most critical task of decompilation, the recovery of object structures, imports, and other relevant information may also be desirable. However, without global or cross-function context, recovering such facets of the program becomes substantially more difficult. We hope future work may explore both alternative chunking strategies and the recovery of supplemental information.

== Re-compilability and Re-executability <sec:discussion:re>

Existing literature in LLM-based decompilation often benchmarks LLM output against re-compilability and re-executability metrics @ref:llm4-decompile @ref:sk2-decomp @ref:salt4-decomp @ref:ref-decompile @ref:decomp-bench. These benchmarks are composed of individual, standalone C functions that compile in isolation with only standard library dependencies, and the LLM receives the entire function's decompilation as input. Re-executability evaluation relies on either manually designed test cases, as in HumanEval-Decompile, which limits scalability, or automatically generated IO examples, as in ExeBench, which may not comprehensively test correctness. 

Such an evaluation paradigm is difficult to replicate for the complex Go projects we include in our benchmark. Unlike C, where a single function with standard includes can compile independently, Go requires a valid package declaration, correct imports, and type-consistent definitions across files. Moreover, because Go binary decompilations far exceed LLM context limits, our pipeline chunks by function---the LLM never sees the full program and therefore cannot produce the package structure, import statements, or cross-file type definitions required for compilation. Re-compiling each Go program from individually decompiled functions would require solving a program synthesis problem orthogonal to decompilation quality. We therefore evaluate our system on neither re-compilability nor re-executability, and instead assess syntactic correctness of each function as a practical proxy, alongside CodeBLEU and LLM-as-a-Judge for semantic similarity.

== String Recovery

Our string recovery heuristics, while effective at surfacing many string literals that would otherwise appear as opaque memory references, have several notable limitations. Phase 1's scan-until-non-printable strategy reads up to 200 bytes from a pointer without knowing the true string length, and frequently over-reads into adjacent strings in the packed `.rodata` block. The LLM thus receives noise concatenated with the actual string and must infer the boundary on its own, which can degrade accuracy. Phases 3 and 4 attempt to mitigate this by detecting paired lengths from adjacent assignments in the decompiled code, but this heuristic is pattern-dependent and fails when the decompiler reorders or optimizes away the length assignment. Moreover, a non-negligible proportion of string references go unresolved: hex literals falling outside `.rodata`, strings constructed dynamically at runtime (e.g. via `fmt.Sprintf`), and symbols that do not conform to Ghidra's `s_*`, `PTR_s_*`, or `DAT_*` naming conventions are missed entirely. We did not measure an overall string recovery rate across the corpus, and leave such quantification to future work.

Unresolved or noisy strings can directly degrade downstream LLM output. For instance, a missing format string may significantly decrease the likelihood of the LLM recovering an `fmt.Sprintf` or `fmt.Errorf` call; a 200-byte packed block annotation where only 7 bytes are relevant may cause the LLM to hallucinate extra string content. Finally, commercial tools such as IDA Pro's GoStrings plugin @ref:idastrings offer more robust Go string recovery, but are restricted behind a paywall; access to such tools may improve the quality of string annotations provided to the LLM.

== Coding Agents

Recent LLM-based coding agents have shown strong results on programming benchmarks @ref:swe-bench @ref:terminal-bench and programming-adjacent benchmarks, e.g. cybersecurity @ref:cybergym. To our knowledge, past work on LLM-based decompilers has not considered the use of such coding agents. We hope to explore the application of such agents in the future, especially considering their demonstrated capabilities working in code repositories with sizes much larger than their available context windows. For instance, a coding agent's global view of the binary and decompilation, although potentially narrow relative to the total content, could prove crucial for LLM-based recovery of supplemental information and strings. Their strong coding abilities may also be helpful for enabling re-compilability.

== Compiler Optimizations

Some evaluation metrics are erroneously low since the Go compiler inlines several functions for optimization, and the evaluation metrics cannot easily note the similarity between a function call and its inlined version, even if the recovery of the function's logic is perfect. We believe its impact is largely negligible, though, since the inlining process impacts a sufficiently small proportion of the total corpus of functions for evaluation.

Other compiler optimizations, such as constant folding or common subexpression elimination, may also affect evaluation in unexpected ways. Nevertheless, we believe such optimizations impact a negligible fraction of our dataset.

== Shared Functions

Many repositories produced more than one binary. For each such repository, their binaries often included the same user-defined functions, typically internal library functions. For the purpose of evaluating each binary in isolation, i.e. to simulate the typical situation where a reverse engineer may gain access to only a single binary, our system applied the LLM to each copy of this function separately. This may slightly bias aggregate metrics toward internal library functions that is frequently shared by different binaries, but we hypothesize that the impact is negligible, considering the large corpus size and that most repositories have merely 1-10 binaries.

As a result of the non-determinism of both the compilation and decompilation process (with regards to a single function as a part of a larger program), the Ghidra decompilations of the same function may differ between each binary. Thus, it may be useful to consider how providing the different Ghidra decompilations as context impacts the LLM’s recovery accuracy. We leave such questions to future work.

== Excluding Imports

We instruct the LLM, in the system prompt, to exclude package declarations and import statements from its output. Since our pipeline performs inference on individual functions in isolation, the LLM can often lack the cross-function and cross-file context necessary to reliably reconstruct a complete and correct import block. And, importantly, our chunked ground truth source code also excludes imports, as each ground truth file contains only the body of a single function or method declaration. Including imports in the LLM output would introduce tokens absent from the ground truth, diluting lexical similarity metrics---particularly for short functions, where a few extraneous import lines could cause substantial reductions in CodeBLEU scores. We acknowledge that import recovery is a meaningful aspect of full program reconstruction, as import paths carry key semantic information (e.g. `context.Context`, `sync.Mutex`) that signals the abstractions in use and contextualizes a function's purpose in the entire program. However, recovering imports is fundamentally a whole-program task that requires knowledge of all types referenced across the entire package, and is thus outside the scope of our per-function evaluation. We leave the recovery of import statements to future work.

== Obfuscation

Our dataset does not include any obfuscated binaries (e.g. obfuscated `pclntab`) and many of ComeBack's heuristics, such as function name recovery and string recovery, may fail when applied to obfuscated Go binaries. In particular, this limits our method's applicability to Go malware, which is often obfuscated.

== Inference vs. Fine-Tuning

Several previous works @ref:llm4-decompile @ref:sk2-decomp @ref:salt4-decomp @ref:ref-decompile @ref:btc fine-tune their own models for decompilation, while others @ref:context-guided-decomp @ref:decgpt use existing models for inference. The detriment of no fine-tuning is that our method loses out on the demonstrated benefits of fine-tuning for the task-specific domain @ref:lora; nonetheless, our approach offers more flexibility to researchers and reverse engineers, as SOTA LLMs like Gemini, Claude, and GPT can be incorporated into our method without any prior modification @ref:context-guided-decomp. This is particularly important considering that recent benchmarks @ref:decompbench demonstrate that SOTA LLMs such as GPT-4o outperform fine-tuned approaches like LLM4Decompile @ref:llm4-decompile and MLM @ref:mlm, largely due to the rapid improvements observed in SOTA LLMs.

== Costs

The inference costs of this project totalled to about \$1,100 in Gemini inference credits. Additionally, we were unable to perform comparisons between our pipeline with Gemini versus other SOTA models, both proprietary and open-source, due to limited resources.

= Conclusion <sec:conclusion>

We created a novel, real-world benchmark for assessing decompilers' capabilities to recover the original Go source code from a Go binary and proposed _ComeBack_, an end-to-end LLM-based decompilation pipeline to reconstruct Go source code. Through qualitative and quantitative evaluation on our dataset, we demonstrate that _ComeBack_ is effective at producing semantically similar, readable Go code to assist the reverse engineering of Go binaries. We will be publishing our dataset to HuggingFace and have open-sourced _ComeBack_ on GitHub #footnote[https://github.com/nightxade/come-back] for public use.

= Acknowledgements <sec:acknowledgements>

We thank Google for sponsoring Gemini inference credits for this project. We also thank David Wagner (UC Berkeley) for his advice and guidance.