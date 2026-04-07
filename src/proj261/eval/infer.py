#!/usr/bin/env python3
"""Send Ghidra decompilations and/or raw binaries to Gemini to recover Go source.

Reads data/metadata.json to discover binaries, then for each one:
  - Optionally uploads the raw binary via the Gemini Files API
  - Loads chunked decomps from data/decomps_chunked/
  - Sends each chunk to Gemini with a mode-appropriate prompt
  - Writes recovered Go source to out/<owner__repo>/<variant>/<binary>/

Modes:
  decomp        — send only the Ghidra decompilation
  binary        — send only the raw binary
  decomp+binary — send both
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from proj261.util import METADATA_PATH, BINARIES_DIR, CHUNKED_DECOMPS_DIR, OUT_DIR, PROJECT_DIR, DEFAULT_MODEL, safe_name

from dotenv import load_dotenv
from google import genai
from google.genai import types
from tqdm import tqdm

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

MODES = ("decomp", "binary", "decomp+binary")

RETRY_BASE = 2       # seconds
RETRY_MULT = 2
RETRY_CAP = 60       # seconds
RETRY_MAX = 5

DEFAULT_MAX_OUTPUT_TOKENS = 65_536


# --------------------------------------------------------------------------- #
#  System prompts
# --------------------------------------------------------------------------- #

_DECOMP_SYSTEM = (
    "You are an expert Go reverse engineer. You will be given a decompiled C "
    "pseudocode file produced by Ghidra from a compiled Go binary. Recover the "
    "original Go source code for ALL functions in the input as accurately as "
    "possible. You MUST recover every function — do not stop early or "
    "summarize. Output ONLY valid Go code with no explanation."
)

_BINARY_SYSTEM = (
    "You are an expert Go reverse engineer. You will be given a compiled Go "
    "binary. Analyze it and recover the original Go source code for ALL "
    "functions as accurately as possible. You MUST recover every function — "
    "do not stop early or summarize. Output ONLY valid Go code with no "
    "explanation."
)

_DECOMP_BINARY_SYSTEM = (
    "You are an expert Go reverse engineer. You will be given a compiled Go "
    "binary AND its Ghidra decompiled C pseudocode. Use both to recover the "
    "original Go source code for ALL functions in the input as accurately as "
    "possible. You MUST recover every function — do not stop early or "
    "summarize. Output ONLY valid Go code with no explanation."
)

SYSTEM_PROMPTS = {
    "decomp": _DECOMP_SYSTEM,
    "binary": _BINARY_SYSTEM,
    "decomp+binary": _DECOMP_BINARY_SYSTEM,
}


def sanitize_func_name(name: str) -> str:
    """Turn a Ghidra function name into a safe filename component."""
    name = name.replace("/", "__")
    name = re.sub(r"[*()\[\]]", "", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name or "unnamed"


def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


def strip_code_fences(text: str) -> str:
    """Remove markdown ```go ... ``` fences from Gemini output."""
    text = re.sub(r"^```(?:go|Go)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text


def get_chunks_for_binary_impl(entry, mode):
    """Load chunked decomps for a binary.

    Returns list of (chunk_name, chunk_source) tuples from the chunked
    decomps directory.  For binary-only mode, returns a single
    ("whole", None) entry.
    """
    if mode == "binary":
        return [("whole", None)]

    chunked_dir = entry.get("chunked_dir")
    if not chunked_dir:
        return []

    chunked_path = Path(chunked_dir)
    manifest_path = chunked_path / "manifest.json"
    if not manifest_path.exists():
        return []

    try:
        manifest = json.loads(manifest_path.read_text())
        chunks = []
        for chunk_info in manifest["chunks"]:
            chunk_file = chunked_path / chunk_info["file"]
            if chunk_file.exists():
                chunk_name = chunk_info["file"].replace(".c", "")
                chunks.append((chunk_name, chunk_file.read_text()))
        return chunks
    except (json.JSONDecodeError, KeyError):
        return []


# --------------------------------------------------------------------------- #
#  Gemini call with retry
# --------------------------------------------------------------------------- #

def call_gemini(
    client: genai.Client,
    model: str,
    system_prompt: str,
    contents: list,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> types.GenerateContentResponse | None:
    """Call Gemini with exponential backoff on retryable errors."""
    delay = RETRY_BASE
    for attempt in range(1, RETRY_MAX + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=max_output_tokens,
                ),
            )
            return response
        except Exception as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            retryable = code in (429, 500, 503)
            # Also retry on common transient message patterns
            if not retryable:
                msg = str(e).lower()
                retryable = any(
                    k in msg for k in ("resource exhausted", "overloaded", "unavailable")
                )
            if retryable and attempt < RETRY_MAX:
                tqdm.write(f"    Retry {attempt}/{RETRY_MAX} after {delay}s: {e}")
                time.sleep(delay)
                delay = min(delay * RETRY_MULT, RETRY_CAP)
            else:
                tqdm.write(f"    ERROR (attempt {attempt}): {e}")
                return None
    return None


# --------------------------------------------------------------------------- #
#  Per-binary processing
# --------------------------------------------------------------------------- #

def process_binary(
    client: genai.Client,
    entry: dict,
    mode: str,
    model: str,
    force: bool,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    call_budget: list[int] | None = None,
) -> dict:
    """Process a single binary: upload, infer chunks, write output.

    Args:
        max_output_tokens: Maximum tokens in each generated response.
        call_budget: If provided, a single-element list [remaining_calls].
            Decremented on each API call; stops early when it reaches 0.

    Returns a result dict for the summary metadata.
    """
    repo = entry["repo"]
    variant = entry["variant"]
    binary = entry["binary"]
    binary_path = Path(entry["binary_path"])

    sname = safe_name(repo)
    out_dir = OUT_DIR / sname / variant / binary
    meta_path = out_dir / "metadata.json"

    # Resumability: skip if already completed
    if not force and meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text())
            if existing.get("status") == "completed":
                return {"binary": binary, "status": "skipped", "reason": "already completed"}
        except json.JSONDecodeError:
            pass

    out_dir.mkdir(parents=True, exist_ok=True)

    system_prompt = SYSTEM_PROMPTS[mode]
    needs_binary = mode in ("binary", "decomp+binary")

    result = {
        "repo": repo,
        "variant": variant,
        "binary": binary,
        "mode": mode,
        "model": model,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "functions": {},
        "status": "in_progress",
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }

    # Upload binary if needed
    uploaded_file = None
    if needs_binary:
        try:
            uploaded_file = client.files.upload(
                file=binary_path,
                config=types.UploadFileConfig(
                    display_name=f"{sname}/{variant}/{binary}",
                    mime_type="text/plain",
                ),
            )
        except Exception as e:
            tqdm.write(f"    Failed to upload binary: {e}")
            result["status"] = "error"
            result["error"] = f"upload failed: {e}"
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            meta_path.write_text(json.dumps(result, indent=2))
            return result

    try:
        # Determine chunks to process
        chunks = get_chunks_for_binary_impl(entry, mode)

        for func_name, code_block in chunks:
            sanitized = sanitize_func_name(func_name)
            out_file = out_dir / f"{sanitized}.go"

            # Per-function resumability
            if not force and out_file.exists() and out_file.stat().st_size > 0:
                result["functions"][func_name] = {"status": "skipped"}
                continue

            # Check call budget
            if call_budget is not None and call_budget[0] <= 0:
                break

            # Build content parts
            contents = []
            if uploaded_file is not None:
                contents.append(uploaded_file)
            if code_block is not None:
                contents.append(code_block)

            response = call_gemini(client, model, system_prompt, contents, max_output_tokens)
            if call_budget is not None:
                call_budget[0] -= 1

            if response is None:
                result["functions"][func_name] = {"status": "error"}
                continue

            # Extract text and token counts
            text = response.text or ""
            text = strip_code_fences(text)

            usage = response.usage_metadata
            in_tok = usage.prompt_token_count if usage else 0
            out_tok = usage.candidates_token_count if usage else 0
            result["total_input_tokens"] += in_tok
            result["total_output_tokens"] += out_tok

            out_file.write_text(text)
            result["functions"][func_name] = {
                "status": "ok",
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            }

    finally:
        # Clean up uploaded file
        if uploaded_file is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass

    # Check if all functions succeeded
    statuses = {v["status"] for v in result["functions"].values()}
    if "error" in statuses:
        result["status"] = "partial"
    else:
        result["status"] = "completed"

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(result, indent=2))
    return result


def run_batch_inference(client, entries, args):
    """Run inference using the Gemini Batch API."""
    print(f"Collecting work for batch inference (model={args.model}, mode={args.mode})...")

    all_work = []
    needed_binaries = set()
    binary_results = {}

    for entry in entries:
        sname = safe_name(entry["repo"])
        variant = entry["variant"]
        binary = entry["binary"]
        out_dir = OUT_DIR / sname / variant / binary

        # Resumability check for binary
        meta_path = out_dir / "metadata.json"
        if not args.force and meta_path.exists():
            try:
                existing = json.loads(meta_path.read_text())
                if existing.get("status") == "completed":
                    continue
            except json.JSONDecodeError:
                pass

        # Determine chunks
        chunks = get_chunks_for_binary_impl(entry, args.mode)

        pending_chunks = []
        for func_name, code_block in chunks:
            sanitized = sanitize_func_name(func_name)
            out_file = out_dir / f"{sanitized}.go"

            if not args.force and out_file.exists() and out_file.stat().st_size > 0:
                continue

            pending_chunks.append((func_name, code_block))

        if pending_chunks:
            all_work.append({
                "entry": entry,
                "chunks": pending_chunks
            })
            if args.mode in ("binary", "decomp+binary"):
                needed_binaries.add(entry["binary_path"])

            binary_results[f"{entry['repo']}|{variant}|{binary}"] = {
                "repo": entry["repo"],
                "variant": variant,
                "binary": binary,
                "mode": args.mode,
                "model": args.model,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "functions": {},
                "status": "in_progress",
                "total_input_tokens": 0,
                "total_output_tokens": 0,
            }

    if not all_work:
        print("Nothing to process (all outputs exist or no matching binaries found).")
        return

    # Apply --max-calls limit
    if args.max_calls is not None:
        remaining = args.max_calls
        trimmed_work = []
        for w in all_work:
            if remaining <= 0:
                break
            if len(w["chunks"]) <= remaining:
                trimmed_work.append(w)
                remaining -= len(w["chunks"])
            else:
                w["chunks"] = w["chunks"][:remaining]
                trimmed_work.append(w)
                remaining = 0
        all_work = trimmed_work

    total_chunks = sum(len(w["chunks"]) for w in all_work)
    print(f"Found {len(all_work)} binaries with {total_chunks} functions/chunks to process.")

    # 1. Upload binaries
    uploaded_binaries = {}
    if needed_binaries:
        print(f"Uploading {len(needed_binaries)} binaries...")
        with ThreadPoolExecutor(max_workers=min(len(needed_binaries), args.threads)) as pool:
            futures = {}
            for bpath in needed_binaries:
                ent = next(w["entry"] for w in all_work if w["entry"]["binary_path"] == bpath)
                display_name = f"{safe_name(ent['repo'])}/{ent['variant']}/{ent['binary']}"
                futures[pool.submit(client.files.upload, path=Path(bpath),
                                    config=types.UploadFileConfig(display_name=display_name))] = bpath

            for future in tqdm(as_completed(futures), total=len(futures), desc="Uploading binaries"):
                bpath = futures[future]
                uploaded_binaries[bpath] = future.result()

    # 2. Prepare JSONL
    print("Preparing batch request JSONL...")
    system_prompt = SYSTEM_PROMPTS[args.mode]
    jsonl_lines = []
    key_map = {}

    for work in all_work:
        entry = work["entry"]
        binary_key = f"{entry['repo']}|{entry['variant']}|{entry['binary']}"

        for func_name, code_block in work["chunks"]:
            key = f"k{len(jsonl_lines):06d}"
            key_map[key] = (binary_key, func_name)

            parts = []
            if args.mode in ("binary", "decomp+binary"):
                up_file = uploaded_binaries[entry["binary_path"]]
                parts.append({"fileData": {"fileUri": up_file.uri, "mimeType": up_file.mime_type}})

            if code_block:
                parts.append({"text": code_block})

            request_obj = {
                "key": key,
                "request": {
                    "contents": [{"role": "user", "parts": parts}],
                    "systemInstruction": {"parts": [{"text": system_prompt}]}
                }
            }
            jsonl_lines.append(json.dumps(request_obj))

    # 3. Upload JSONL
    tmp_path = None
    jsonl_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
            tmp.write("\n".join(jsonl_lines) + "\n")
            tmp_path = Path(tmp.name)

        print("Uploading batch requests file...")
        jsonl_file = client.files.upload(path=tmp_path, config=types.UploadFileConfig(display_name="batch_requests.jsonl"))

        # 4. Start Batch
        print(f"Submitting batch job (model={args.model})...")
        batch_job = client.batches.create(
            model=args.model,
            src=jsonl_file.name
        )
        print(f"Batch job created: {batch_job.name}")

        # 5. Poll
        start_time = time.time()
        while True:
            job = client.batches.get(name=batch_job.name)
            status = job.state
            elapsed = int(time.time() - start_time)

            if status == "JOB_STATE_SUCCEEDED":
                print(f"\nBatch job succeeded after {elapsed}s!")
                break
            elif status == "JOB_STATE_FAILED":
                print(f"\nBatch job failed after {elapsed}s: {job.error}")
                return
            elif status == "JOB_STATE_CANCELLED":
                print(f"\nBatch job cancelled after {elapsed}s.")
                return

            sys.stdout.write(f"\rStatus: {status} ({elapsed}s elapsed)...")
            sys.stdout.flush()
            time.sleep(min(60, 10 + elapsed // 10)) # Adaptive sleep

        # 6. Process results
        print("Downloading and processing results...")
        output_file_name = job.output.file_name
        content = client.files.download(name=output_file_name)

        for line in content.decode().strip().split("\n"):
            if not line: continue
            res_obj = json.loads(line)
            key = res_obj["key"]
            binary_key, func_name = key_map[key]

            res_meta = binary_results[binary_key]
            entry = next(w["entry"] for w in all_work if f"{w['entry']['repo']}|{w['entry']['variant']}|{w['entry']['binary']}" == binary_key)
            out_dir = OUT_DIR / safe_name(entry["repo"]) / entry["variant"] / entry["binary"]
            out_dir.mkdir(parents=True, exist_ok=True)

            sanitized = sanitize_func_name(func_name)
            out_file = out_dir / f"{sanitized}.go"

            if "response" in res_obj:
                resp = res_obj["response"]
                try:
                    text = ""
                    for cand in resp.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            if "text" in part:
                                text += part["text"]

                    text = strip_code_fences(text)
                    out_file.write_text(text)

                    usage = resp.get("usageMetadata", {})
                    in_tok = usage.get("promptTokenCount", 0)
                    out_tok = usage.get("candidatesTokenCount", 0)

                    res_meta["functions"][func_name] = {
                        "status": "ok",
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                    }
                    res_meta["total_input_tokens"] += in_tok
                    res_meta["total_output_tokens"] += out_tok
                except Exception as e:
                    print(f"Error processing {func_name}: {e}")
                    res_meta["functions"][func_name] = {"status": "error", "error": str(e)}
            else:
                err = res_obj.get("status", {})
                print(f"Function {func_name} failed: {err.get('message', 'Unknown error')}")
                res_meta["functions"][func_name] = {"status": "error", "error": err.get("message")}

        # 7. Finalize and write metadata
        for binary_key, res_meta in binary_results.items():
            statuses = {v["status"] for v in res_meta["functions"].values()}
            if "error" in statuses:
                res_meta["status"] = "partial"
            else:
                res_meta["status"] = "completed"

            res_meta["finished_at"] = datetime.now(timezone.utc).isoformat()

            entry = next(w["entry"] for w in all_work if f"{w['entry']['repo']}|{w['entry']['variant']}|{w['entry']['binary']}" == binary_key)
            meta_path = OUT_DIR / safe_name(entry["repo"]) / entry["variant"] / entry["binary"] / "metadata.json"
            meta_path.write_text(json.dumps(res_meta, indent=2))

    finally:
        # Cleanup
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()
        if jsonl_file:
            try:
                client.files.delete(name=jsonl_file.name)
            except: pass
        for up in uploaded_files.values():
            try:
                client.files.delete(name=up.name)
            except: pass

    print_summary(list(binary_results.values()))


# --------------------------------------------------------------------------- #
#  Entry collection
# --------------------------------------------------------------------------- #

def collect_entries(meta: dict, mode: str, args) -> list[dict]:
    """Build a flat list of binaries to process, respecting filters."""
    entries = []
    needs_decomp = mode in ("decomp", "decomp+binary")
    needs_binary = mode in ("binary", "decomp+binary")

    for repo_name, info in meta["repos"].items():
        if args.repo and repo_name != args.repo:
            continue
        if not info.get("cloned") or not info.get("compiled_at"):
            continue

        sname = safe_name(repo_name)
        for variant, bin_list in info.get("binaries", {}).items():
            if args.variant and variant != args.variant:
                continue
            for bin_name in bin_list:
                binary_path = BINARIES_DIR / sname / variant / bin_name
                chunked_dir = CHUNKED_DECOMPS_DIR / sname / variant / bin_name

                # Check prerequisites
                if needs_binary and not binary_path.exists():
                    continue
                if needs_decomp and not (chunked_dir / "manifest.json").exists():
                    continue

                # Size filter
                if args.max_size and binary_path.exists():
                    if binary_path.stat().st_size > args.max_size * 1_000_000:
                        continue

                entry = {
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "binary_path": str(binary_path),
                    "chunked_dir": str(chunked_dir),
                }

                entries.append(entry)

    # Limit repos
    if args.max_repos:
        seen: dict[str, None] = {}
        filtered = []
        for e in entries:
            if e["repo"] not in seen:
                if len(seen) >= args.max_repos:
                    continue
                seen[e["repo"]] = None
            filtered.append(e)
        entries = filtered

    return entries


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Send Ghidra decompilations / binaries to Gemini to recover Go source.",
    )
    parser.add_argument("--mode", required=True, choices=MODES,
                        help="What to send: decomp, binary, or decomp+binary")
    parser.add_argument("--repo", type=str, default=None,
                        help="Filter to a specific repo (e.g. ollama/ollama)")
    parser.add_argument("--variant", type=str, default=None,
                        help="Filter to a specific variant (default, debug, stripped)")
    parser.add_argument("--max-repos", type=int, default=None,
                        help="Limit number of repos to process")
    parser.add_argument("--max-size", type=int, default=None,
                        help="Skip binaries larger than N MB")
    parser.add_argument("--threads", type=int, default=1,
                        help="Parallel threads for synchronous mode or binary uploads (default: 1)")
    parser.add_argument("--force", action="store_true",
                        help="Re-run even if output already exists")
    parser.add_argument("--max-calls", type=int, default=None,
                        help="Limit total number of LLM inference calls")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS,
                        help=f"Max output tokens per response (default: {DEFAULT_MAX_OUTPUT_TOKENS:,})")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL,
                        help=f"Gemini model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--no-batch", action="store_true",
                        help="Use synchronous API instead of Batch API")
    args = parser.parse_args()

    # Load API key
    load_dotenv(PROJECT_DIR / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set. Copy .env.example to .env and fill in your key.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    # Collect work
    meta = load_metadata()
    entries = collect_entries(meta, args.mode, args)

    if not args.no_batch:
        run_batch_inference(client, entries, args)
        return

    # Synchronous mode (filtered for resumability)
    if not args.force:
        pending = []
        for e in entries:
            sname = safe_name(e["repo"])
            meta_path = OUT_DIR / sname / e["variant"] / e["binary"] / "metadata.json"
            if meta_path.exists():
                try:
                    existing = json.loads(meta_path.read_text())
                    if existing.get("status") == "completed":
                        continue
                except json.JSONDecodeError:
                    pass
            pending.append(e)
        entries = pending

    if not entries:
        print("Nothing to process (all outputs exist or no matching binaries found).")
        return

    n_threads = max(1, args.threads)
    call_budget = [args.max_calls] if args.max_calls is not None else None

    print(f"Processing {len(entries)} binaries with model={args.model}, "
          f"mode={args.mode}, threads={n_threads} (SYNC MODE)")

    results = []

    def _do_one(entry):
        return process_binary(client, entry, args.mode, args.model, args.force,
                              args.max_output_tokens, call_budget)

    if n_threads == 1:
        for entry in tqdm(entries, desc="Inferring", unit="bin"):
            if call_budget is not None and call_budget[0] <= 0:
                break
            tqdm.write(f"  {entry['repo']}  {entry['variant']}/{entry['binary']}")
            results.append(_do_one(entry))
    else:
        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futures = {pool.submit(_do_one, e): e for e in entries}
            for future in tqdm(as_completed(futures), total=len(entries),
                               desc="Inferring", unit="bin"):
                entry = futures[future]
                tqdm.write(f"  {entry['repo']}  {entry['variant']}/{entry['binary']}")
                try:
                    results.append(future.result())
                except Exception as exc:
                    tqdm.write(f"    THREAD ERROR: {exc}")
                    results.append({"binary": entry["binary"], "status": "error"})

    # Summary
    print_summary(results)


def print_summary(results):
    completed = sum(1 for r in results if r.get("status") == "completed")
    partial = sum(1 for r in results if r.get("status") == "partial")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    errored = sum(1 for r in results if r.get("status") == "error")
    total_in = sum(r.get("total_input_tokens", 0) for r in results)
    total_out = sum(r.get("total_output_tokens", 0) for r in results)

    print(f"\n{'='*60}")
    print(f"  Completed:  {completed}")
    print(f"  Partial:    {partial}")
    print(f"  Skipped:    {skipped}")
    print(f"  Errors:     {errored}")
    print(f"  Tokens in:  {total_in:,}")
    print(f"  Tokens out: {total_out:,}")
    print(f"  Output dir: {OUT_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
