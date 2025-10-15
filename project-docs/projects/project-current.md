# PROJECT-CURRENT – Codex Toolbox Rollout

> Primary project coordinating Codex MCP tooling, planning utilities, and document ingestion improvements described in temp4.md and temp5.md.

## Project Structure
- **Project Key:** `PROJECT-CURRENT`
- **Tasks:** All active backlog items (tooling fixes, planning utilities, ingestion updates) live under this project in ParadeDB.
- **Plan DAG:** `intellibot/project-docs/task-dags/dag-project-current.md` (ingest via Prefect to keep dependencies current).
- **Documentation Flags:** Marked `is_project_doc=true`, `is_dev=true` for development scope.

## Workflow
1. Use MCP task tools (`list_projects`, `list_tasks`, `list_open_tasks`) to inspect project backlog.
2. Update the DAG markdown when task ordering changes; ingest with `ingest_document` so search + references pick it up.
3. Apply tool discovery flows (tool search, agent catalog) per task requirements; record tool/task mappings back into the DAG doc.
4. When closing the project, capture a completion summary and ingest it as `project-current-completion.md`.

