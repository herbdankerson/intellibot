"""Send a test request to Gemini using the failover client."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.my_agentic_chatbot.llm_calls.gemini_client import GeminiFailoverClient

MAX_OUTPUT_TOKENS = 65_536
MAX_THINKING_BUDGET = 32_768


def normalize_thinking_budget(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    presets = {
        "low": 512,
        "medium": 2_048,
        "high": 8_192,
        "max": MAX_THINKING_BUDGET,
        "auto": -1,
        "dynamic": -1,
        "off": 0,
        "none": 0,
    }
    value: Optional[int]
    try:
        key = raw.strip().lower()
    except AttributeError:
        key = None
    if key and key in presets:
        value = presets[key]
    else:
        try:
            value = int(raw) if raw is not None else None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid thinking budget: {raw}") from exc
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute a Gemini 2.5 Pro test call")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="Say hello from the failover client in one sentence.",
    )
    parser.add_argument(
        "--model",
        default="gemini/gemini-2.5-pro",
        help="Gemini model identifier to use",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=MAX_OUTPUT_TOKENS,
        help=(
            "Maximum output tokens to request (<=65,536 for gemini-2.5-pro)."
        ),
    )
    parser.add_argument(
        "--thinking-budget",
        default="max",
        help=(
            "Thinking token budget or preset (low/medium/high/max/auto)."
        ),
    )
    parser.add_argument(
        "--include-thoughts",
        action="store_true",
        help="Request thought summaries in the response payload.",
    )
    args = parser.parse_args()

    os.environ.setdefault("GEMINI_KEY_VAULT_PATH", "gapistash2")
    try:
        thinking_budget = normalize_thinking_budget(args.thinking_budget)
    except ValueError as exc:
        parser.error(str(exc))
    if args.max_tokens > MAX_OUTPUT_TOKENS:
        parser.error(
            f"Gemini 2.5 Pro outputs at most {MAX_OUTPUT_TOKENS} tokens; requested {args.max_tokens}."
        )
    if (
        thinking_budget is not None
        and thinking_budget not in (-1, 0)
        and thinking_budget > MAX_THINKING_BUDGET
    ):
        parser.error(
            "Thinking budget exceeds 32,768 tokens, the documented cap for gemini-2.5-pro."
        )

    client = GeminiFailoverClient(model=args.model)
    response = client.completion(
        messages=[{"role": "user", "content": args.prompt}],
        temperature=0.0,
        max_output_tokens=args.max_tokens,
        thinking_budget=thinking_budget,
        include_thoughts=args.include_thoughts,
    )
    fields = getattr(response, "provider_specific_fields", {}) or {}
    text = fields.get("fallback_text")
    if text is None:
        text = client._extract_text(response)  # type: ignore[attr-defined]
    print("=== Gemini 2.5 Pro Response ===")
    if text:
        print(text)
    else:
        print(response)


if __name__ == "__main__":
    main()
