# Microsoft Agent Framework Migration Plan

## Goals

- Replace the Prefect-based runtime orchestrator with a Microsoft Agent Framework (AF) workflow that implements a deterministic, loop-aware state machine.
- Preserve the existing KB schema, MCP tool adapters, LiteLLM routing, and Prefect-based ETL/approvals pipeline.
- Add an acceptance-aware loop that repeatedly evaluates requirement coverage and revises the plan until quality bars are satisfied or stop conditions trigger.
- Keep API surface (`WorkflowOrchestrator`) and tooling contracts compatible with the rest of the service and tests.

## High-Level Architecture

```
Planner ──▶ AF Workflow (state machine) ──▶ Evidence Pack ──▶ Responder ──▶ Audit
            │                                │                         │
            ├─ Tool execution loop ◀──────────┴─ Gap evaluator ◀────────┤
            └─ Plan revision (LLM) ──────────────────────────────────────┘
```

### Workflow Stages (Executors)

1. **InitializeRun** – seeds shared state (`PlanLoopState`) with the user message, plan, and budgets. Handles plan HITL approval.
2. **SelectNextAction** – inspects outstanding requirements and decides whether to execute tasks, revise the plan, or finish.
3. **ExecuteTasks** – runs MCP/agent tools via `ToolSuite` for the currently targeted requirements. Generates raw evidence + provisional findings.
4. **CurateEvidence** – deduplicates, clips, and writes approved evidence back into shared state. Also persists findings.
5. **EvaluateGaps** – computes requirement satisfaction metrics (agreement × authority × extraction quality) and acceptance rubric coverage. Emits open questions for unmet requirements.
6. **RevisePlan** – when gaps remain, calls `revise_plan_with_evidence` to emit an updated task graph and merges it into state. Maintains iteration count + stop conditions.
7. **Respond** – assembles an `EvidencePack`, pushes through the responder, and runs the audit gate + final HITL approval.
8. **Finalize** – emits `ExecutionResult` with audit + acceptance summary and terminates the workflow.

The workflow uses AF `WorkflowBuilder` edges to loop between executors 2–6 until all requirements pass the acceptance gate or iteration limits are hit.

### Shared State (`PlanLoopState`)

Fields:
- `user_message`, `plan`, `iteration`, `max_iterations`
- `evidence: list[EvidenceItem]`, `findings: list[Finding]`
- `requirement_status: dict[str, RequirementEvaluation]`
- `open_questions: list[OpenQuestion]`
- `report: ExecutionReport`, `notes: list[str]`
- `needs_revision: bool`, `terminated: bool`, `termination_reason`

### Acceptance Evaluation

For each requirement we compute:
- `supporting_evidence_ids` (≥ required count from distinct sources)
- `authority_score` (domain heuristics + metadata flags)
- `agreement_score` (# of corroborating sources)
- `quality_score` (finding confidence, snippet length, parser cleanliness)
- `confidence = clamp(weighted_sum)`
- `issues`: missing corroboration, low authority, conflicts

Requirement considered satisfied when:
- `confidence ≥ threshold` (default 0.75)
- `len(supporting_sources) ≥ min_sources` (default 2; 3 for “high/strict” quality bars)

Unmet requirements produce `OpenQuestion` entries to feed the planner revision prompt.

### Iteration & Stop Conditions

- Default `max_iterations = 4` (configurable via settings).
- Workflow terminates early if:
  - Planner HITL reject
  - Task policy violation repeated beyond retry
  - Acceptance gate continues to fail after `max_iterations` (flagged as `insufficient_evidence`).

### Integration Points

- `ToolSuite` remains the adapter layer for MCP servers and custom agents.
- Responder, AuditAgent, Approver classes stay intact; orchestrator coordinates them.
- Prefect remains for ETL scripts and scheduling; orchestration runtime no longer depends on it.
- Docker image simply installs `agent-framework==1.0.0b251001` (preview). No other services change.

## Implementation Plan

1. **Dependencies & Container**
   - Add `agent-framework==1.0.0b251001` (and sub-packages) to `requirements.txt` & `pyproject.toml`.
   - Ensure Docker base installs build essentials if needed; re-pin `numpy` wheels to avoid compilation.

2. **State & Acceptance Modules**
   - Create `src/my_agentic_chatbot/workflows/af/state.py` with `PlanLoopState`, `RequirementEvaluation`, helper enums.
   - Create `src/my_agentic_chatbot/workflows/af/acceptance.py` implementing the confidence calculation + gating.

3. **Executors & Workflow**
   - Implement AF executors in `src/my_agentic_chatbot/workflows/af/executors.py`.
   - Rewrite `WorkflowOrchestrator` to build/run the AF workflow, preserving public methods (`execute_plan`, `run_pipeline`).
   - Maintain backward-compatible `ToolSuite`, `assemble_evidence`, and `run_tool` helpers where practical.

4. **Plan Revision Loop**
   - Integrate `planner.revise_plan_with_evidence` in the `RevisePlan` executor.
   - Store iteration history and ensure tasks merge/dedupe when plan is revised.

5. **Testing**
   - Update `tests/test_orchestrator.py` for the new iterative behavior and acceptance gating.
   - Add unit tests for acceptance scoring + loop termination edge cases.

6. **Documentation**
   - Update README with Agent Framework notes + container instructions.
   - Document new settings (iteration limits, thresholds) in `instructions.md` if relevant.

## Web Curation + KB Ingestion

- Introduced `agent.web_work_items` staging table (run-scoped, TTL) for raw search hits and curated captures.
- `WebTool` now performs `search → fetch/render → summarize → ingest` for each candidate URL:
  - Clears the staging table for the current run, logs SearxNG hits, and calls Sequential Thinking for gap heuristics.
  - Fetches HTML (httpx) with Playwright MCP fallback, converts to Markdown (readability + markdownify), and scores authority/topicality/locality.
  - Summarizes relevance with Gemini, converts HTML→Markdown→chunks, and persists to ParadeDB via `ingest_web_capture` (shared ETL helpers).
  - Records curated artefacts (KB document + chunk IDs) back into `agent.web_work_items` and returns the highest scored snippets as evidence.
- Acceptance defaults tightened (`min_sources` base=2, strict=3; `confidence_threshold`=0.75) so the AF loop keeps iterating until curated evidence meets quality bars.

## Risks & Mitigations

- **Preview SDK changes** – pin to known beta build and expose single upgrade flag in requirements.
- **Dependency bloat** – use selective import to avoid heavy optional extras (DevUI, Copilot Studio) unless required.
- **Loop stalls** – enforce deterministic fallback (limit iterations, escalate to unresolved questions in final answer).
- **Performance** – reuse existing dedupe/snippet utilities; clip evidence before returning to responder.
