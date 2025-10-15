# Tool: `tool_search_tools`

## Overview
- Queries `cfg.tools` metadata in ParadeDB to locate candidate tools by slug, description, manifest reference, or notes.
- Supports planners choosing capabilities dynamically (ReAct / sequential) without hard-coding tool lists.
- Returns slug, manifest reference, default parameter schema, and enablement state.

## Inputs
- `query` *(string, required)* – Keyword search applied against slug, manifest reference, and notes.
- `limit` *(integer, optional, default 10)* – Maximum number of matching tools to return.
- `include_disabled` *(boolean, optional, default false)* – When true, returns disabled rows for auditing.

## Outputs
- `results` *(array)* – Each element includes:
  - `slug` *(string)* – Registry identifier for the tool.
  - `kind` *(string)* – Tool type (`native`, `mcp`, `http`, etc.).
  - `manifest_or_ref` *(string)* – Python callable or remote manifest reference.
  - `default_params` *(object)* – Schema hints and default arguments captured at registration.
  - `enabled` *(boolean)* – Indicates whether the tool is currently available.
  - `notes` *(string, optional)* – Additional guidance or restrictions.

## Usage Notes
- Pair this output with `tool_manifest_fetch` to retrieve full documentation and example invocations.
- When results reference Prefect flows (e.g., `freshbot.executors.prefect:execute_flow`), use the Prefect execution tool to run them.
- For dynamic planning, add tool slugs and schemas to the planner’s memory before executing steps.
- `include_disabled=true` assists with debugging registry drift; ensure you respect the enable flag before calling a tool.

## Related Tools & Flows
- `tool_manifest_fetch` – Retrieves linked README markdown for the selected tool.
- `tool_prefect_flow_catalog` – Shows deployments you can trigger once a Prefect-backed tool is selected.
- `tool_graph_capabilities_map` – Visualises relationships between tools, agents, and flows (Neo4j).

