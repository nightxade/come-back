"""LLM-as-a-judge comparison metric using the Gemini API.

Asks the model to rate semantic similarity between the original Go source
and the inferred Go source on a 0--10 scale.  Optionally includes an
explanation alongside the score.
"""

import json
import os
import re
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types
from tqdm import tqdm

from proj261.util import PROJECT_DIR, DEFAULT_MODEL

# --------------------------------------------------------------------------- #
#  Module-level state (set by configure())
# --------------------------------------------------------------------------- #

_client: genai.Client | None = None
_model: str = DEFAULT_MODEL
_explain: bool = False

RETRY_BASE = 2
RETRY_MULT = 2
RETRY_CAP = 60
RETRY_MAX = 5

# --------------------------------------------------------------------------- #
#  Prompts
# --------------------------------------------------------------------------- #

_SYSTEM_SCORE_ONLY = """\
You are an expert Go programmer evaluating decompilation quality.
You will be given two Go functions: the ORIGINAL source and an INFERRED
reconstruction produced by an LLM from a Ghidra decompilation.

Rate the semantic similarity on a 0-10 integer scale:
  0  = completely different / nonsensical
  5  = partially correct logic with significant differences
  10 = semantically identical

Respond with ONLY a JSON object: {"score": <int>}"""

_SYSTEM_WITH_EXPLANATION = """\
You are an expert Go programmer evaluating decompilation quality.
You will be given two Go functions: the ORIGINAL source and an INFERRED
reconstruction produced by an LLM from a Ghidra decompilation.

Rate the semantic similarity on a 0-10 integer scale:
  0  = completely different / nonsensical
  5  = partially correct logic with significant differences
  10 = semantically identical

Respond with ONLY a JSON object:
{"score": <int>, "explanation": "<brief explanation of the rating>"}"""


def _build_prompt(source: str, inferred: str) -> str:
    return (
        f"## Original Go source\n```go\n{source}\n```\n\n"
        f"## Inferred Go source\n```go\n{inferred}\n```"
    )


# --------------------------------------------------------------------------- #
#  Gemini call with retry
# --------------------------------------------------------------------------- #

def _call_gemini(system_prompt: str, contents: str) -> str | None:
    """Call Gemini with exponential backoff.  Returns response text or None."""
    delay = RETRY_BASE
    for attempt in range(1, RETRY_MAX + 1):
        try:
            response = _client.models.generate_content(
                model=_model,
                contents=[contents],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=1024,
                    temperature=0.0,
                ),
            )
            return response.text or ""
        except Exception as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            retryable = code in (429, 500, 503)
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
                tqdm.write(f"    LLM judge ERROR (attempt {attempt}): {e}")
                return None
    return None


def _parse_response(text: str) -> dict | None:
    """Extract JSON from the model response, tolerating markdown fences."""
    text = text.strip()
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object anywhere in the text
        match = re.search(r"\{[^}]+\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


# --------------------------------------------------------------------------- #
#  Comparator interface
# --------------------------------------------------------------------------- #

def add_args(parser):
    """Register LLM-judge-specific CLI arguments."""
    parser.add_argument(
        "--explain", action="store_true",
        help="Ask the LLM to provide an explanation alongside the score",
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL,
        help=f"Gemini model for LLM judge (default: {DEFAULT_MODEL})",
    )


def configure(args):
    """Initialize the Gemini client and store settings."""
    global _client, _model, _explain

    load_dotenv(PROJECT_DIR / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit(
            "Error: GEMINI_API_KEY not set. Copy .env.example to .env and fill in your key."
        )

    _client = genai.Client(api_key=api_key)
    _model = args.model
    _explain = args.explain


def compare_functions(
    source: str,
    inferred: str,
    decomp: str,
    metadata: dict,
) -> dict:
    """Ask Gemini to rate semantic similarity between source and inferred code."""
    system = _SYSTEM_WITH_EXPLANATION if _explain else _SYSTEM_SCORE_ONLY
    prompt = _build_prompt(source, inferred)

    text = _call_gemini(system, prompt)
    if text is None:
        return {"score": -1, "error": "api_call_failed"}

    parsed = _parse_response(text)
    if parsed is None:
        return {"score": -1, "error": "parse_failed", "raw": text}

    score = parsed.get("score")
    if not isinstance(score, (int, float)) or score < 0 or score > 10:
        return {"score": -1, "error": "invalid_score", "raw": text}

    result = {"score": score / 10.0}
    if _explain:
        result["explanation"] = parsed.get("explanation", "")
    return result


def aggregate(results: list[dict]) -> dict:
    """Compute mean score, excluding errors."""
    valid = [r for r in results if r.get("score", -1) >= 0]
    errors = len(results) - len(valid)
    if not valid:
        return {"mean_score": 0.0, "num_scored": 0, "num_errors": errors}
    mean = sum(r["score"] for r in valid) / len(valid)
    return {"mean_score": round(mean, 4), "num_scored": len(valid), "num_errors": errors}
