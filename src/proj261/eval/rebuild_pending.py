#!/usr/bin/env python3
"""Rebuild pending_batches.json from a JSON list of already-submitted Gemini batch jobs.

Use this when infer.py crashed after submitting some/all batches but before
saving pending_batches.json. Provide a JSON file of batch entries
(job_name, create_time, region) in the same order the batches were submitted.

The script replicates the exact same collect/group logic as infer.py's
submit_batch_inference so the key_map entries match what Gemini received.
If fewer names are provided than batch groups, only those groups are recorded.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from proj261.util import METADATA_PATH, PRED_DIR, DEFAULT_MODEL, safe_name
from proj261.eval.infer import (
    collect_entries,
    get_chunks_for_binary_impl,
    SYSTEM_PROMPT,
    _load_pending,
    _save_pending,
    _MAX_BATCH_FILE_BYTES,
)


def build_batch_groups(entries, args):
    """Replicate the collect/group logic from submit_batch_inference.

    Returns (batch_groups, binary_results) where batch_groups is a list of
    (lines, key_map, binary_keys) and binary_results is the full dict keyed
    by binary_key.
    """
    all_work = []
    binary_results = {}

    for entry in entries:
        sname = safe_name(entry["repo"])
        variant = entry["variant"]
        binary = entry["binary"]
        out_dir = PRED_DIR / sname / variant / binary
        meta_path = out_dir / "metadata.json"

        if not args.force and meta_path.exists():
            try:
                existing = json.loads(meta_path.read_text())
                if existing.get("status") == "completed":
                    continue
            except json.JSONDecodeError:
                pass

        chunks = get_chunks_for_binary_impl(entry)
        pending_chunks = []
        for rel_path, code_block in chunks:
            out_file = out_dir / f"{rel_path}.go"
            if not args.force and out_file.exists() and out_file.stat().st_size > 0:
                continue
            pending_chunks.append((rel_path, code_block))

        if pending_chunks:
            binary_key = f"{entry['repo']}|{variant}|{binary}"
            all_work.append({"entry": entry, "chunks": pending_chunks})
            binary_results[binary_key] = {
                "repo": entry["repo"],
                "variant": variant,
                "binary": binary,
                "model": args.model,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "functions": {},
                "status": "in_progress",
                "total_input_tokens": 0,
                "total_output_tokens": 0,
            }

    if not all_work:
        return [], binary_results

    if args.max_calls is not None:
        remaining = args.max_calls
        trimmed = []
        for w in all_work:
            if remaining <= 0:
                break
            if len(w["chunks"]) <= remaining:
                trimmed.append(w)
                remaining -= len(w["chunks"])
            else:
                w["chunks"] = w["chunks"][:remaining]
                trimmed.append(w)
                remaining = 0
        all_work = trimmed

    # Build per-binary JSONL data with byte sizes (mirrors submit_batch_inference exactly)
    per_binary_data = []
    global_idx = 0

    for work in all_work:
        entry = work["entry"]
        binary_key = f"{entry['repo']}|{entry['variant']}|{entry['binary']}"
        lines = []
        km = {}
        byte_size = 0

        for rel_path, code_block in work["chunks"]:
            key = f"k{global_idx:06d}"
            global_idx += 1
            km[key] = [binary_key, rel_path]

            request_obj = {
                "key": key,
                "request": {
                    "contents": [{"role": "user", "parts": [{"text": code_block}]}],
                    "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                },
            }
            line = json.dumps(request_obj)
            lines.append(line)
            byte_size += len(line.encode("utf-8")) + 1

        per_binary_data.append((lines, km, binary_key, byte_size))

    # Group binaries into size-limited batches at binary boundaries
    batch_groups = []
    cur_lines: list[str] = []
    cur_km: dict = {}
    cur_bkeys: list[str] = []
    cur_size = 0

    for lines, km, bkey, bsize in per_binary_data:
        if cur_lines and cur_size + bsize > _MAX_BATCH_FILE_BYTES:
            batch_groups.append((cur_lines, cur_km, cur_bkeys))
            cur_lines, cur_km, cur_bkeys, cur_size = [], {}, [], 0
        cur_lines.extend(lines)
        cur_km.update(km)
        cur_bkeys.append(bkey)
        cur_size += bsize

    if cur_lines:
        batch_groups.append((cur_lines, cur_km, cur_bkeys))

    return batch_groups, binary_results


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild pending_batches.json from a pre-existing list of Gemini batch job names.",
    )
    parser.add_argument(
        "batches_json",
        type=Path,
        help=(
            "JSON file of ordered batch entries: "
            '[{"job_name": "batches/...", "create_time": "...", "region": "..."}, ...]'
        ),
    )
    parser.add_argument("--repo", type=str, nargs="*", default=None,
                        help="Filter to specific repo(s) (must match what was passed to infer)")
    parser.add_argument("--variant", type=str, default=None)
    parser.add_argument("--max-repos", type=int, default=None)
    parser.add_argument("--max-binaries", type=int, default=None)
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--force", action="store_true",
                        help="Treat already-completed binaries as needing inference (match infer --force)")
    args = parser.parse_args()

    if not args.batches_json.exists():
        print(f"Error: {args.batches_json} not found.")
        sys.exit(1)

    batch_info_list = json.loads(args.batches_json.read_text())
    if not isinstance(batch_info_list, list):
        print("Error: batches JSON must be a list.")
        sys.exit(1)
    if not batch_info_list:
        print("Error: batches JSON list is empty.")
        sys.exit(1)

    print("Collecting work and computing batch groups (this may take a moment)...")
    meta = json.loads(METADATA_PATH.read_text())
    entries = collect_entries(meta, args)

    batch_groups, binary_results = build_batch_groups(entries, args)

    if not batch_groups:
        print("Nothing to process (all outputs exist or no matching binaries found).")
        return

    total_groups = len(batch_groups)
    available = len(batch_info_list)
    n_to_write = min(total_groups, available)

    if available < total_groups:
        print(
            f"Warning: {total_groups} batch group(s) computed but only {available} "
            f"name(s) provided. Writing {n_to_write} entry/entries; "
            f"{total_groups - n_to_write} group(s) will NOT be recorded."
        )
    elif available > total_groups:
        print(
            f"Note: {available} name(s) provided but only {total_groups} batch group(s) "
            f"computed. Extra names will be ignored."
        )

    pending = _load_pending()

    for i in range(n_to_write):
        lines, km, bkeys = batch_groups[i]
        info = batch_info_list[i]

        if "job_name" not in info:
            print(f"Error: entry {i} in batches JSON is missing 'job_name'. Stopping.")
            break

        batch_br = {bk: binary_results[bk] for bk in bkeys}
        pending.append({
            "job_name": info["job_name"],
            "model": args.model,
            "submitted_at": info.get("create_time", datetime.now(timezone.utc).isoformat()),
            "region": info.get("region"),
            "num_items": len(lines),
            "key_map": km,
            "binary_results": batch_br,
        })

    _save_pending(pending)

    written = sum(1 for e in pending if any(
        e["job_name"] == batch_info_list[i]["job_name"] for i in range(n_to_write)
        if i < len(batch_info_list) and "job_name" in batch_info_list[i]
    ))
    total_chunks = sum(len(lines) for lines, _, _ in batch_groups[:n_to_write])
    print(
        f"Wrote {n_to_write}/{total_groups} batch group(s) to pending_batches.json "
        f"({total_chunks} requests total)."
    )
    print("Run `infer --retrieve` to check and download results.")


if __name__ == "__main__":
    main()
