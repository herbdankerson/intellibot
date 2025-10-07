Understood. We’ll lock a **no-literals policy** and convert everything to **variables/config lookups**. Below is the exact, non-code checklist for your IDE to execute—**no hard-coding anywhere** once a variable is defined.

---

# NO-LITERALS POLICY — IMPLEMENTATION STEPS

## 1) Single source of truth (no duplication)

* **Runtime config lives in Postgres**: `cfg.models`, `cfg.tools`, `cfg.policies`, `cfg.agents`, `cfg.prompts`, and a **key-value** table `cfg.active`.
* **Environment variables** hold secrets/endpoints only; DB stores **references** (names/keys), not secrets.

**Acceptance:**

* There is **no second active config** (no duplicated YAML runtime values).
* Startup fails if any required key in `cfg.active` is missing.

---

## 2) Define the required keys (cfg.active)

Create these keys (values are **names**, not URLs; the URLs live in `cfg.models` or `cfg.tools`):

* `active_planner_model`
* `active_responder_model`
* `active_worker_model`
* `active_emb_general`
* `active_emb_legal`
* `active_emb_code` *(keep as-is for now)*

**Acceptance:**

* `SELECT key,value FROM cfg.active;` returns all keys above (non-NULL, non-empty).

---

## 3) Model registry (cfg.models) (no literals in code)

For each model/encoder/chat:

* `name` (e.g., `emb-gte-large`, `emb-legal-bert`, `planner-smollm2`, `responder-smollm2`)
* `provider` (e.g., `text-embeddings-inference`, `ollama`)
* `uri` = **environment-referenced** (e.g., `${TEI_GTE_LARGE_URL}`), not a literal
* `dims` (for embeddings)
* `purpose` (`embedding|chat|classifier`)
* `enabled`, `version`, `notes`

**Acceptance:**

* No code contains model URIs or dims; all are read from `cfg.models`.
* `.env` (or secrets manager) contains `TEI_GTE_LARGE_URL`, `TEI_LEGAL_BERT_URL`, etc., not the code.

---

## 4) Tool registry (cfg.tools)

Register every tool (Search Toolbox, Neo4j helpers, web ingest, doc ingest) with:

* `name` (canonical name)
* `type` (`http|db|graph|local`)
* `endpoint` = **env-referenced** (e.g., `${SEARCH_TOOLBOX_BASEURL}`)
* `method`, `auth` (as **ref** to secret/env var), `timeout_s`, `config` JSON
* `enabled` flag

**Acceptance:**

* No code contains raw tool endpoints; they’re resolved from `cfg.tools`.
* Secrets are not in DB; only references are.

---

## 5) AF loader (strict)

At startup the runtime MUST:

* Read `cfg.active` keys (planner/responder/emb_*).
* Resolve each to a row in `cfg.models` (and retrieve `uri`, `dims`, `provider`).
* Resolve allowed tools for each agent from `cfg.agents.tool_allow` → `cfg.tools`.
* **Fail fast** if any lookup is missing/disabled.

**Acceptance:**

* Removing a key from `cfg.active` or disabling a model in `cfg.models` breaks startup by design.

---

## 6) LiteLLM mappings (variables only)

* `ops/litellm/config.yaml` **must use env vars** for model URLs (e.g., `${TEI_GTE_LARGE_URL}`, `${TEI_LEGAL_BERT_URL}`), **not** hardcoded `http://tei-...`.
* Expose **aliases** only (`emb-general`, `emb-legal`, `emb-code`) and point them to the **env-based URIs**.
* Remove `emb-law`.

**Acceptance:**

* Grep the repo for `http://tei-` → **0 hits** (outside docs).
* `curl /v1/embeddings` with `model=emb-general` returns 1024; with `model=emb-legal` returns 768.

---

## 7) Ingestion & embedding code (variables only)

* **Never** call `'emb-general'` or `'emb-legal'` literals in code.
* The pipeline must read **active encoder names** from `cfg.active` (`active_emb_general`, `active_emb_legal`, `active_emb_code`), then resolve to `cfg.models` to get URIs/dims.
* Embed logic:

  * All chunks → `active_emb_general`
  * `domain=legal` → also `active_emb_legal`
  * `domain=code` → also `active_emb_code`
* Classifiers: unchanged; route via config/tool registry.

**Acceptance:**

* Search for `'emb-general'|'emb-legal'|'emb-code'` in code paths → only allowed in config/SQL migrations, never in runtime logic.
* A runtime log line prints which **active** encoders were resolved for each job (names, dims), not literals.

---

## 8) Search Toolbox (variables only)

* The Toolbox reads `active_emb_general` and conditional `active_emb_legal/active_emb_code` to decide spaces.
* It **never** compares against hardcoded names; it uses resolved active names.
* Logs must include `spaces_used` (these are **resolved names**, not literals in code).

**Acceptance:**

* Toolbox unit test shows it selects spaces based on `cfg.active` ONLY.
* `spaces_used` in logs match `cfg.active` values.

---

## 9) DB schema & indexes (no literals in DDL/maintenance)

* Embedding spaces in `kb_embedding_space` use `name` values matching `cfg.models.name`.
* Maintenance scripts must **resolve space_id by name at runtime** (no hardcoded numeric IDs).
* HNSW indexes are **partial** per `space_id`, created by looking up name → id.

**Acceptance:**

* Migration scripts read `SELECT id FROM kb_embedding_space WHERE name=(active name)`; no literal IDs in DDL.
* Reindex and purge scripts resolve names to IDs first.

---

## 10) Clean slate (test data)

* Drop and recreate only `kb` + `agent` schemas (empty), leave `cfg` intact.
* Re-embed from zero using **resolved active encoder names**.
* Build HNSW indexes AFTER first write per space (resolved by name).

**Acceptance:**

* Row counts present per space; sample vectors have non-zero norms.
* Index names exist and reflect per-space filters.

---

## 11) Observability (variables only in logs)

* Each run/event log must print the **resolved** model/encoder names and dimensions.
* No literal hostnames/URLs in logs; print config names and a short ref (e.g., `provider: tei`).

**Acceptance:**

* Grep logs for `http://` → sanitized/absent; show `name/provider`, not raw URLs.

---

## 12) Anti-regression checks (CI/grep)

* CI step: **reject** commits that introduce literals for:

  * `tei-` host URLs, `emb-general`, `emb-legal`, `emb-code` in code paths.
* CI step: **assert** that `cfg.active` contains required keys before runtime tests.

**Acceptance:**

* A failing test demonstrates CI blocks literals added to runtime code; passing test shows config-only usage.

---

## 13) Documentation (one-pager)

* “**No-Literals Policy**”:

  * Runtime reads `cfg.active` keys and resolves via `cfg.models` / `cfg.tools`.
  * Endpoints and secrets are env-only, referenced by name in DB.
  * Swapping a model = **one** `UPDATE cfg.active` (or env change for endpoint), not code.

**Acceptance:**

* The doc exists; the examples use keys/refs only.

---

### Final sanity

* Change `active_emb_general` to another encoder in DB → runtime uses it next boot **without** any code or YAML edits.
* Change `${TEI_GTE_LARGE_URL}` in env → runtime uses the new endpoint **without** code changes.
* No module contains hardcoded model names, dims, or URLs.

This enforces exactly what you asked: **once a variable is set, it’s the only thing used.**
