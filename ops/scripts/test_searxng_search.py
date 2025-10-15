#!/usr/bin/env python3
"""Quick smoke test for the local SearXNG instance.

Run a query against the running container and print the primary fields the
UI will care about (URL, title, snippet, etc.). Helpful for verifying the
payload when iterating on a search results table.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from typing import Iterable

try:
    import requests
except ImportError as exc:  # pragma: no cover - defensive guard
    raise SystemExit(
        "The 'requests' package is required. Install it into your venv first."
    ) from exc

DEFAULT_BASE_URL = "http://127.0.0.1:8085"


def clean_snippet(snippet: str | None, *, width: int = 100) -> str:
    if not snippet:
        return ""
    snippet = " ".join(snippet.split())
    return textwrap.shorten(snippet, width=width, placeholder="…")


def print_section(title: str, lines: Iterable[str]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for line in lines:
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the local SearXNG instance")
    parser.add_argument("query", help="search terms to send to SearXNG")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Base URL of the SearXNG instance (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--engine",
        default=None,
        help="Optional comma-separated list of engines to constrain the search",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="How many results to print (default: 5)",
    )

    args = parser.parse_args()

    params = {"q": args.query, "format": "json"}
    if args.engine:
        params["engines"] = args.engine

    try:
        response = requests.get(
            f"{args.base_url.rstrip('/')}/search",
            params=params,
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(f"Request to SearXNG failed: {exc}") from exc

    payload = response.json()
    results = payload.get("results", [])
    total = payload.get("number_of_results")
    print_section(
        "Query Overview",
        [
            f"Query echo: {payload.get('query')}",
            f"Returned results: {len(results)} (SearXNG reported total: {total})",
            f"Engines queried: {', '.join(sorted(payload.get('engines', []))) if payload.get('engines') else 'not provided'}",
            f"Suggestions: {', '.join(payload.get('suggestions', [])) or 'none'}",
            f"Corrections: {', '.join(payload.get('corrections', [])) or 'none'}",
            f"Infobox count: {len(payload.get('infoboxes', []))}",
            f"Answer count: {len(payload.get('answers', []))}",
            f"Unresponsive engines: {', '.join(payload.get('unresponsive_engines', [])) or 'none'}",
        ],
    )

    if not results:
        warning = ""
        if args.engine and 'google' in args.engine.split(','):
            warning = (
                " (google currently returns an empty payload because the public HTML layout "
                "now ships inside an obfuscated script; enable the Custom Search API or use "
                "a proxy such as startpage)"
            )
        print(f"\nNo web results returned.{warning}")
        sys.exit(0)

    keys = sorted({key for result in results for key in result})
    print_section("Result object keys", [", ".join(keys)])

    limit = max(args.limit, 0)
    for idx, result in enumerate(results[:limit], start=1):
        fields = [
            f"Title: {result.get('title')}",
            f"URL: {result.get('url')}",
            f"Snippet: {clean_snippet(result.get('content'))}",
            f"Primary engine: {result.get('engine')}",
            f"All engines: {', '.join(result.get('engines', [])) or 'n/a'}",
            f"Published: {result.get('publishedDate') or 'n/a'}",
            f"Score: {result.get('score') if result.get('score') is not None else 'n/a'}",
            f"Category: {result.get('category') or 'n/a'}",
            f"Thumbnail: {result.get('thumbnail') or 'n/a'}",
            f"Template: {result.get('template') or 'n/a'}",
        ]
        print_section(f"Result #{idx}", fields)


if __name__ == "__main__":
    main()
