# Gemini Key Failover Playbook

The `gapistash2` vault holds multiple Gemini API keys that we need to cycle
through whenever Google enforces model-specific daily quotas. This guide spells
out how to wire those keys into LiteLLM so we automatically fail over when the
active key is exhausted, keep track of which key is on cooldown, and safely
re-enable keys after 24 hours.

The workflow has three moving parts:

1. **Key registry** – we parse `gapistash2`, capture the metadata (which
   models a key is allowed to use and its order in the rotation), and store that
   metadata in Postgres so multiple LiteLLM workers share the same view.
2. **LiteLLM router** – a small wrapper that, before each Gemini call, asks the
   registry for “the next usable key for `<model>`” and injects it. When Gemini
   responds with a quota error, we mark that key as exhausted for that specific
   model. Other models can keep using the key until they exhaust their own
   quotas.
3. **Cooldown reset** – a scheduled Prefect flow that walks the registry every
   hour (configurable) and clears any key/model pair whose `available_after`
   timestamp is in the past so the key slips back into the rotation exactly
   24 hours after it was sidelined.

---

## 1. Key registry schema (Postgres)

Add the following tables to the storage schema (append to
`src/my_agentic_chatbot/storage/models.sql`). These tables keep secrets out of
Postgres – we only store labels that refer back to the encrypted key material
inside `gapistash2`.

```sql
CREATE TABLE IF NOT EXISTS llm_key_registry (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    key_label TEXT NOT NULL,
    priority INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (provider, model, key_label)
);

CREATE TABLE IF NOT EXISTS llm_key_state (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    key_label TEXT NOT NULL,
    exhausted_at TIMESTAMPTZ,
    available_after TIMESTAMPTZ,
    last_error TEXT,
    PRIMARY KEY (provider, model, key_label)
);
```

### Populating the registry from `gapistash2`

1. Store Gemini keys in `gapistash2` using an `.env`-style naming scheme that
   encodes the target model and rotation order:

   ```env
   GEMINI_KEY__gemini-1.5-flash__01=sk-...
   GEMINI_KEY__gemini-1.5-flash__02=sk-...
   GEMINI_KEY__gemini-1.5-pro__01=sk-...
   ```

   *Everything after the final `=` stays encrypted at rest in the vault – do
   not commit the file.*

2. Run `ops/scripts/register_gemini_keys.py --file gapistash2` (script stub
   lives alongside the other ops utilities) to upsert rows into
   `llm_key_registry`. The script:

   - Parses the variable name, derives `model` and `priority` from the suffix
     (e.g. `__01` → priority `1`).
   - Stores only the `key_label` (e.g. `GEMINI_KEY__gemini-1.5-flash__01`).
   - Does **not** copy the raw secret into Postgres; LiteLLM will look up the
     label in `gapistash2` at runtime.

3. During registration every key/model gets a sibling row in `llm_key_state`
   with `exhausted_at = NULL` so it enters the pool immediately.

---

## 2. LiteLLM integration

LiteLLM already supports key rotation via its `Router`. We wrap it with a
bespoke `GeminiKeyManager` that understands our registry.

1. **Key manager** (`src/my_agentic_chatbot/llm_calls/gemini_key_manager.py`):
   - On startup, load `gapistash2` (path comes from env var
     `GEMINI_KEY_VAULT_PATH`, defaults to `./gapistash2`).
   - Provide `acquire_key(model: str) -> GeminiKey` which:
     1. Refreshes the Postgres state (`llm_key_state`) and filters out any
        entries whose `available_after > now()`.
     2. Sorts the remaining keys for the model by `priority` and returns the
        first one.
     3. Resolves the actual secret by reading the matching line from
        `gapistash2`.
   - Provide `mark_exhausted(model: str, key_label: str, *, reason: str)` to set
     `exhausted_at = now()` and `available_after = now() + interval '24 hours'`
     for that model/key pair.

2. **Router hook** (`ops/litellm/router_gemini_key_manager.py`):
   - Define `async def before_request(model, kwargs)` that swaps in the secret:

     ```python
     key = key_manager.acquire_key(model)
     kwargs["api_key"] = key.secret
     kwargs.setdefault("metadata", {})["key_label"] = key.label
     ```

   - Define `async def on_error(model, kwargs, error)` that checks for Google’s
     `429`/quota errors and calls `mark_exhausted`.

3. **LiteLLM config (`ops/litellm/config.yaml`)**:

   ```yaml
   models:
     - name: gemini-flash
       model: google/gemini-1.5-flash
       callbacks:
         - ops.litellm.router_gemini_key_manager:GeminiKeyRouter
     - name: gemini-pro
       model: google/gemini-1.5-pro
       callbacks:
         - ops.litellm.router_gemini_key_manager:GeminiKeyRouter
   ```

   The callback is responsible for injecting the key right before LiteLLM
   reaches out to Google.

4. **Running the proxy**:

   ```bash
   export GEMINI_KEY_VAULT_PATH=/secure/path/gapistash2
   export DATABASE_URL=postgresql://...
   litellm --config ops/litellm/config.yaml --port 4000
   ```

The rest of the stack continues to talk to LiteLLM exactly as before. Every
Gemini request now goes through the key manager first.

---

## 3. Cooldown reset (Prefect)

Add a flow at `ops/scripts/reset_exhausted_gemini_keys.py` similar to:

```python
from datetime import datetime, timezone

from prefect import flow

from src.my_agentic_chatbot.storage.db import get_engine


@flow(name="reset-gemini-keys")
def reset_gemini_keys() -> None:
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            UPDATE llm_key_state
               SET exhausted_at = NULL,
                   available_after = NULL,
                   last_error = NULL
             WHERE provider = 'gemini'
               AND available_after IS NOT NULL
               AND available_after <= :now
            """,
            {"now": now},
        )


if __name__ == "__main__":
    reset_gemini_keys()
```

Schedule it hourly in Prefect (`prefect deployment create ...`) or wire it into
`pg_cron` if you prefer keeping everything inside Postgres. Either way, the
flow simply clears any cooldowns whose timer has elapsed – no secrets involved.

---

## Operational checklist

1. **Add/Edit keys** – drop a new line into `gapistash2`, following the naming
   convention. Higher sequence numbers equate to lower priority.
2. **Sync registry** – run `register_gemini_keys.py` to upsert the labels and
   reset their state.
3. **Run LiteLLM** – ensure the proxy process can read both the database and
   the vault file. LiteLLM will take care of issuing requests with the active
   key and flipping to the next one on quota errors.
4. **Monitor** – Prefect flow metrics show how often keys hit quota. The
   LiteLLM request metadata includes the `key_label` so you can add Grafana or
   Honeycomb dashboards later.

This approach keeps the actual key material confined to `gapistash2`, gives us a
central source of truth for key rotation, and allows new keys to join the pool
simply by appending a line and re-running the sync script.
