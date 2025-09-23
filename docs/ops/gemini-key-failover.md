# Gemini Key Failover

The LiteLLM proxy exposes a router callback hook named `gemini_failover` that can
promote Google Gemini 2.5 Pro as the secondary provider when the primary OpenAI
model fails. The callback placeholder lives in `ops/litellm/callbacks/gemini_failover.py`
so ops teams can extend it without touching application code.

To enable the callback:

1. Add valid Gemini API credentials to `.env` (`GOOGLE_API_KEY`).
2. Mount the repository's `ops/litellm` directory inside the LiteLLM container.
3. Implement retry/failover logic in `gemini_failover` as needed.
4. Restart the LiteLLM service so the config and callback module are reloaded.

The LiteLLM configuration already includes the `gemini-fallback` model and registers
the callback. Once the logic is filled in, the proxy will route failed planner or
responder calls through Gemini automatically.
