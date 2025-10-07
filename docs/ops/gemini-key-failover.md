# Gemini Key Failover

LiteLLM now handles Gemini failover using its native router logic—no custom
callback code required. Each Gemini-backed virtual model (`planner`,
`responder`, `cheap-worker`, and the embedding aliases) is declared multiple
times in `ops/litellm/config.yaml`, once per API key. When a call returns a
quota or rate error, LiteLLM automatically retries with the next key in the
list.

## Configure the key pool

1. Add your Gemini keys to `.env` using the numbered variables:
   - `GOOGLE_API_KEY` (primary)
   - `GOOGLE_API_KEY_1` ... `GOOGLE_API_KEY_5`
   - Extend the list if you have more keys by following the same pattern.
2. Restart the LiteLLM service (`docker compose restart litellm`) so the proxy
   picks up the refreshed configuration.

The router is locked to Gemini 2.5 models:

- `gemini/gemini-2.5-pro` powers the `planner` and `responder` agents.
- `gemini/gemini-2.5-flash` backs the `cheap-worker` helper.
- `gemini/embedding-001` handles all embedding aliases.

With `litellm_settings.num_retries` set, the proxy will transparently fail over
across the configured keys before surfacing an error to clients.
