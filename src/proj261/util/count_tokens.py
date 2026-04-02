# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "google-genai",
#     "python-dotenv",
# ]
# ///
"""Utility to count tokens for a file using the Gemini API.

Supports both text (source code, decompilations) and binary files.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from proj261.util.paths import PROJECT_DIR

def is_binary(file_path: Path) -> bool:
    """Check if a file is likely binary by looking for null bytes in the first 1KB."""
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(1024)
            return b'\x00' in chunk
    except Exception:
        return True

def main():
    parser = argparse.ArgumentParser(description="Count Gemini tokens for a file.")
    parser.add_argument("file", help="Path to the file (text or binary)")
    parser.add_argument("--model", default="gemini-2.0-flash-lite", 
                        help="Gemini model to use for counting (default: gemini-2.0-flash-lite)")
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: File {args.file} not found.")
        sys.exit(1)

    load_dotenv(PROJECT_DIR / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set in .env.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    try:
        if is_binary(file_path):
            print(f"File '{args.file}' looks binary. Uploading temporarily to count tokens...")
            # Upload to File API (required for binary token counting)
            uploaded_file = client.files.upload(
                file=file_path,
                config=types.UploadFileConfig(display_name=f"token_count_{file_path.name}", mime_type="text/plain"),
            )
            try:
                contents = [types.Part.from_uri(
                    file_uri=uploaded_file.uri,
                    mime_type=uploaded_file.mime_type
                )]
                resp = client.models.count_tokens(model=args.model, contents=contents)
            finally:
                # Always cleanup the temporary upload
                client.files.delete(name=uploaded_file.name)
        else:
            # Text file: count directly
            text = file_path.read_text(errors='replace')
            resp = client.models.count_tokens(model=args.model, contents=text)

        print(f"\nFile:   {args.file}")
        print(f"Model:  {args.model}")
        print(f"Tokens: {resp.total_tokens:,}")

    except Exception as e:
        print(f"Error counting tokens: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
