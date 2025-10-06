You’re right—hard-coding per-topic schemas is a dead end. The planner needs to discover what’s needed from the prompt + acceptance criteria, spin up the right tasks, ask for more when it’s thin, and curate for the responder. Here’s how to make that happen without domain-specific schemas.

---

## The core idea: a generic, planner-driven loop (no per-domain schema)

Give the Planner a universal JSON contract it can always emit—slots it discovers, not slots we predefine:

### Minimal, domain-agnostic outputs

* ProblemSpec: what the user asked; constraints/acceptance criteria it extracted.
* Requirements[]: dynamically discovered info needs (each has `question`, `priority`, `stop_when_found`, `quality_bar`).
* Tasks[]: concrete tool calls proposed to satisfy requirements (with budgets/timeouts).
* Findings[]: normalized key–value “claims” and their evidentiary support.
* OpenQuestions[]: what’s still missing + suggested follow-up tasks.
* StopConditions: when to stop or escalate.

This keeps autonomy, avoids handcrafting schemas, and gives the Workflow Agent enough structure to execute, retry, and curate.

---

## Planner loop (autonomous, requirement-driven)

1. Extract requirements & rubric from the user prompt (+ any acceptance criteria you pass in).
2. Propose tasks per requirement (prioritized).
3. Execute tasks → collect raw results.
4. Curate results into Findings[]: key/value claims with source IDs + confidence.
5. Evaluate gap vs. requirements (rubric) → if missing/low confidence, auto-append follow-ups.
6. Repeat until StopConditions (met, budget/time, or diminishing returns).

No judge-schema, no per-domain types—just Requirements/Tasks/Findings/OpenQuestions.

---

## Concretely: what to change (file-by-file)

### 1) Planner: make it discover, not assume

**File:** `src/my_agentic_chatbot/planner/main_planner.py`

* Add a PlannerSystemPrompt that forces this JSON shape (ProblemSpec, Requirements, Tasks, StopConditions).

* Implement `plan_from_message(message, prior_findings=None, prior_open_questions=None) -> dict` that:

  * Extracts Requirements[] (questions to answer; priority; quality_bar).
  * Emits Tasks[] mapping to tools (`web`, `db_keyword`, `db_vector`, `graph`) with budgets/timeouts.
  * Includes StopConditions (e.g., “if all priority-1 requirements have ≥2 indep sources & confidence ≥0.7”).

* Add `revise_plan_with_evidence(plan, new_evidence) -> dict` to:

  * Promote Findings[] from new evidence.
  * Remove satisfied requirements; create follow-up Tasks for gaps (auto-ask for more).
  * Adjust tools (e.g., escalate to `graph` if cross-checking required).

**Done when:** the Planner always returns actionable tasks + a gap-aware follow-up list—no domain schema.

---

### 2) Tools: answer requirements, not just “do a search”

**Files:**

* `src/my_agentic_chatbot/tools/web_tools.py`
* `src/my_agentic_chatbot/tools/db_tools.py`
* `src/my_agentic_chatbot/tools/graph_tools.py`

Upgrade each tool adapter to accept a Requirement (question + quality_bar) and return Findings candidates:

* `web_tools.search_and_curate(requirement, budget)`

  * Expand queries, fetch, extract atomic facts (key/value), attach snippet, heading_path, URL; estimate confidence (source type × agreement × recency); write to KB, return `EvidenceItem[]` and a list of candidate Findings `{key, value, confidence, evidence_ids[]}`.
* `db_tools.curated_search(requirement, boost_entity=None)`

  * BM25 + vector RRF, dedupe, return snippets and extracted candidate Findings.
* `graph_tools.cross_check(findings)`

  * Optional: verify consistency / link duplicates; lift agreement/confidence.

**Done when:** every tool returns Findings candidates tied to EvidenceItem IDs, not just raw snippets.

---

### 3) Orchestrator: become the execution brain for the Planner loop

**File:** `src/my_agentic_chatbot/workflows/orchestrator.py`

Implement a deterministic loop:

1. `plan = plan_from_message(message)`
2. Approval #1 (optional, keep it)
3. For each Task in priority order:

   * Call the tool adapter, get `EvidenceItem[]` + `candidate_findings[]`, append to pack.
4. Summarize & curate: merge duplicate findings, compute confidence (agreement × authority × snippet quality), enforce EvidencePack ≤ 4k tokens (summarize at boundary).
5. Evaluate gap: if `plan.StopConditions` not met → `plan = revise_plan_with_evidence(plan, curated_findings)` and loop (respect budgets/timeouts).
6. Approval #2 (evidence review) → call Responder with curated Findings/Evidence.
7. Approval #3 → finalize.

**Done when:** the flow automatically asks for more when the evidence is thin and stops when requirements are truly met.

---

### 4) Responder: consume Findings, not ad-hoc snippets

**File:** `src/my_agentic_chatbot/response/responder.py`

* Prompt consumes Findings[] + EvidencePack; instructs: “Only assert claims present in Findings; cite by `EvidenceItem.id`.”
* If acceptance criteria still unmet, respond with what’s missing and recommend next tasks (mirrors OpenQuestions).

---

### 5) Data layer: keep it generic

**Files:**

* `src/my_agentic_chatbot/schemas.py` → define generic types only:

  * `Requirement { question:str, priority:int, quality_bar:str }`
  * `Task { id, for_requirement:int, tool:Literal['web','db_keyword','db_vector','graph'], inputs:dict, budget_tokens:int, timeout_s:int }`
  * `Finding { key:str, value:str, confidence:float, evidence_ids:list[str] }`
  * `OpenQuestion { question:str, reason:str }`
  * `Plan { problem_spec:str, requirements:list[Requirement], tasks:list[Task], stop_conditions:list[str] }`
  * Keep `EvidenceItem`/`EvidencePack` as you specced.

No judge schema. No domain types. All dynamic.

---

## How this changes behavior on your failed “Judge Brewer” run

* Planner extracts requirements itself (e.g., “Who is the judge?”, “Education?”, “Prior career?”, “Division/case types?”, “Reputation/temperament?”, “Notable reversals?”) with a quality bar (“≥2 independent sources for education”).
* First pass tasks: web + db. If results are directory garbage, Findings confidence stays low, so the planner adds follow-ups: “site:ballotpedia.org", “site:jud12.flcourts.org", “site:flcourts.org", "background" OR "education" OR "procedures", then fetch/extract again.
* Tools write to KB and return candidate Findings so the orchestrator can compute agreement and keep looping until StopConditions or budget.

That’s autonomy. No per-topic handholding. It “asks for more” on its own.

---

## Very specific edits to make it real (checklist)

1. `planner/main_planner.py`

   * Add new planner system prompt and implement `plan_from_message()`, `revise_plan_with_evidence()`.
   * Plan JSON uses only `ProblemSpec`, `Requirements[]`, `Tasks[]`, `StopConditions[]`.
2. `tools/web_tools.py`

   * Implement: `search_and_curate(requirement, budget)` → expands queries, fetches pages, extracts atomic facts, indexes to KB, returns `(evidence_items, candidate_findings)`.
3. `tools/db_tools.py`

   * Implement: `curated_search(requirement, boost_entity=None)` → BM25+vector RRF, returns `(evidence_items, candidate_findings)`.
4. `tools/graph_tools.py`

   * Optional cross-check/boost: `cross_check(findings)` and entity upsert for future searches.
5. `workflows/orchestrator.py`

   * Implement loop: Plan → Execute tasks → Curate findings → Evaluate → Revise plan (repeat) → Responder.
6. `response/responder.py`

   * Enforce “only Findings” claims + citation validation.
7. `schemas.py`

   * Add `Requirement`, `Finding`, `OpenQuestion` alongside your existing `Evidence*` models.

---

## Guardrails that keep this efficient (and not chatty)

* Caps everywhere: per-tool N results, per-page chunks, EvidencePack ≤ 4k, timeouts (8–15s).
* Confidence model: `(agreeing sources × authority × extract quality)`; require ≥ threshold per requirement’s `quality_bar`.
* Retry budget: allow 1–2 follow-up waves; stop if diminishing returns (no new Findings or rising duplicates).
* KB-first bias: after first web pass, prefer DB curated results (BM25+vector) to avoid re-fetching and keep tokens cheap.

---

## If you want me to write the stubs next

Say “ship stubs,” and I’ll produce:

* `planner/main_planner.py` with the exact prompt + JSON validators.
* `web_tools.search_and_curate()` and `db_tools.curated_search()` skeletons (including RRF logic, snippet extractor, and KB write-backs).
* `workflows/orchestrator.py` with the loop, gap evaluation, and stop logic.

This keeps the system autonomous and generic: the Planner discovers needs, tools try to answer the questions, the loop asks for more when thin, and the Responder speaks only from curated Findings.
