import argparse
import asyncio
import json
import os
import sys
import traceback
from typing import Any, Dict, Optional

try:
    import google.generativeai as genai
except Exception as e:
    print(f"Error importing google.generativeai: {e}")
    traceback.print_exc()
    sys.exit(1)

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client


MAX_OUTPUT_TOKENS = 65_536
MAX_THINKING_BUDGET = 32_768


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a Gemini-powered tool agent against the Postgres MCP server."
    )
    parser.add_argument(
        "instruction",
        nargs="+",
        help="User instruction to send to the agent.",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-pro",
        help="Gemini model identifier (default: gemini-2.5-pro)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Maximum output tokens (<= 65,536 for gemini-2.5-pro).",
    )
    parser.add_argument(
        "--thinking-budget",
        default="auto",
        help=(
            "Thinking token budget or preset (low/medium/high/max/auto)."
        ),
    )
    parser.add_argument(
        "--include-thoughts",
        action="store_true",
        help="Request thought summaries from the Gemini response.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature for the model (default: 0.0).",
    )
    parser.add_argument(
        "--response-mime-type",
        default="text/plain",
        help="Desired response MIME type passed to the model.",
    )
    return parser


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
    try:
        key = raw.strip().lower()
    except AttributeError:
        key = None
    if key and key in presets:
        return presets[key]
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid thinking budget: {raw}") from exc


def build_generation_config(
    *,
    max_output_tokens: Optional[int],
    temperature: float,
    thinking_budget: Optional[int],
    include_thoughts: bool,
    response_mime_type: Optional[str],
) -> Any:
    config: Dict[str, Any] = {}
    if temperature is not None:
        config["temperature"] = temperature
    if max_output_tokens is not None:
        config["max_output_tokens"] = max_output_tokens
    if response_mime_type:
        config["response_mime_type"] = response_mime_type
    thinking_config: Dict[str, Any] = {}
    if thinking_budget is not None:
        thinking_config["thinking_budget"] = thinking_budget
    if include_thoughts:
        thinking_config["include_thoughts"] = True
    if thinking_config:
        config["thinking_config"] = thinking_config

    # Prefer native client dataclasses when available.
    generation_cls = getattr(genai, "GenerationConfig", None)
    thinking_cls = getattr(genai, "ThinkingConfig", None)
    if generation_cls is not None:
        kwargs = dict(config)
        if "thinking_config" in kwargs and thinking_cls is not None:
            kwargs["thinking_config"] = thinking_cls(**kwargs["thinking_config"])
        try:
            return generation_cls(**kwargs)
        except TypeError:
            # Fall back to dict if installed version differs.
            pass
    return config


async def main():
    """Connects to the postgres-mcp server and lists the available tools."""

    parser = build_parser()
    args = parser.parse_args()
    instruction = " ".join(args.instruction)

    try:
        thinking_budget = normalize_thinking_budget(args.thinking_budget)
    except ValueError as exc:
        parser.error(str(exc))

    if args.max_output_tokens is not None and args.max_output_tokens > MAX_OUTPUT_TOKENS:
        parser.error(
            f"Gemini 2.5 Pro outputs at most {MAX_OUTPUT_TOKENS} tokens; requested {args.max_output_tokens}."
        )
    if (
        thinking_budget is not None
        and thinking_budget not in (-1, 0)
        and thinking_budget > MAX_THINKING_BUDGET
    ):
        parser.error(
            "Thinking budget exceeds 32,768 tokens, the documented cap for gemini-2.5-pro."
        )

    generation_config = build_generation_config(
        max_output_tokens=args.max_output_tokens,
        temperature=args.temperature,
        thinking_budget=thinking_budget,
        include_thoughts=args.include_thoughts,
        response_mime_type=args.response_mime_type,
    )

    # Configure Gemini API key
    try:
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    except KeyError:
        print("Please set the GEMINI_API_KEY environment variable.")
        return

    # Connect to the postgres-mcp server and get the list of tools
    async with sse_client("http://localhost:8000/sse") as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()

            # Create a prompt for the Gemini model
            prompt = f"""
System: You are a helpful assistant that can use tools to answer questions about a PostgreSQL database.

Available tools:
{json.dumps(tools.model_dump(), indent=2)}

User: {instruction}

Tool call: 
"""

            # Generate a tool call using the Gemini model
            generation_kwargs: Dict[str, Any] = {}
            if generation_config:
                generation_kwargs["generation_config"] = generation_config
            model = genai.GenerativeModel(args.model)
            response = await model.generate_content_async(prompt, **generation_kwargs)

            try:
                tool_call = json.loads(response.text)
            except json.JSONDecodeError:
                print(f"Error: Invalid JSON response from the model: {response.text}")
                return

            # Execute the tool call
            tool_result = await session.call_tool(
                tool_call["name"],
                tool_call.get("arguments", {}),
            )

            # Print the result as JSON
            print(json.dumps(tool_result.model_dump(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
