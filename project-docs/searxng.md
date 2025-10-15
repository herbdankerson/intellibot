# SearXNG Web Search Notes

## Current Instance (intellibot-searxng-1)
- Container image: `searxng/searxng:latest` (2025.6.10+ build pulled 2025-10-11).
- Config volume: `intellibot_searxng-config` providing `/etc/searxng/settings.yml`.
- Redis backing limiter/cache at `intellibot-searxng-redis-1`.

## JSON Payload Shape
A `GET http://127.0.0.1:8085/search?q=<query>&format=json` returns:

```json
{
  "query": "<echoed query>",
  "number_of_results": 31,
  "results": [
    {
      "url": "https://example.com/article",
      "title": "Example title",
      "content": "Snippet text...",
      "publishedDate": null,
      "thumbnail": "",
      "engine": "brave",
      "template": "default.html",
      "parsed_url": ["https", "example.com", "/", "", "", ""],
      "img_src": "",
      "priority": "",
      "engines": ["startpage", "duckduckgo", "brave"],
      "positions": [1, 1, 1],
      "score": 9.0,
      "category": "general"
    }
  ],
  "suggestions": [],
  "corrections": [],
  "answers": [],
  "infoboxes": [],
  "unresponsive_engines": []
}
```

The `results[*].engines` array lists every upstream engine that contributed a matching document. The primary source for ranking appears in `results[*].engine`.

## Google Engine Status
- Requests routed through `engines=google` receive HTTP 200 responses from Google but return **zero parsed results**. Google now serves the search response inside an obfuscated `google.pmc` script payload without the former `SC7lYd` result blocks that the upstream parser expects (SearXNG issue: [google html changes](https://github.com/searxng/searxng/issues) — no fix in 2025.6 line).
- Adding alternate parameters (`udm=14`, `gbv=1`, mobile user agents, `noj=1`, `async=_fmt:json`) either produces CAPTCHA redirects (`sorry.google.com`) or the same script-only payload.
- The stock parser (`searx/engines/google.py`) skips every result because it cannot locate `<div jscontroller="SC7lYd">` markup anymore.

### Required Configuration for Real Google Data
1. **Official API:** Configure a Google Programmable Search Engine and enable the Custom Search API for the project. Supply both `GOOGLE_API_KEY` and `GOOGLE_CX` (search engine ID). The `google` module does not ship with API support, so either:
   - Implement a custom engine module (e.g., `google_cse.py`) that calls `https://www.googleapis.com/customsearch/v1` with those credentials, or
   - Proxy the API through LiteLLM/OpenAI proxy if you plan to expose it to agents.
2. **Serp-proxy alternative:** Integrate a Google proxy such as Startpage or Brave. Startpage is already enabled and returns Google-sourced links without CAPTCHAs; results show `engine: startpage` but originate from Google SERPs.
3. **Scraping approach:** If continuing with direct scraping, plan to patch `searx/engines/google.py` to decode `google.pmc` payloads (the data now sits in nested JSON and requires additional parsing). Until that parser is updated upstream, direct Google scraping will remain empty.

## Test Script
`ops/scripts/test_searxng_search.py` exercises the local instance:

```bash
./ops/scripts/test_searxng_search.py "climate tech" --limit 5          # default multi-engine run
./ops/scripts/test_searxng_search.py "climate tech" --engine startpage # force Startpage (Google proxy)
./ops/scripts/test_searxng_search.py "climate tech" --engine google    # currently yields 0 results
```

The script prints:
- Query overview (counts, engines queried, infobox/answer counts, suggestions, unresponsive engines)
- Union of keys present in the result objects
- Per-result summary (title, URL, snippet, engines, score, category, thumbnail/template metadata)

## Next Steps
- Decide whether to pursue the Google Custom Search API route (requires enabling the API for the existing Google Cloud project and capturing a `cx` ID) or continue via Startpage.
- If sticking with scraping, keep an eye on upstream SearXNG updates for the new Google layout and port the patch once published.
- Integrate the script output into the SearXNG search-results table to validate column mapping (URL, title, snippet, score, primary engine, categories, etc.).
