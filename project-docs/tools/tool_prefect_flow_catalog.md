# Tool: `tool_prefect_flow_catalog`

## Overview
- Parses `freshbot/src/freshbot/flows/flows.yaml` to enumerate Prefect deployments available to the system.
- Returns callable paths, deployment names, tags, and work pool/queue metadata.
- Bridges planning and execution by pointing directly to flows runnable via the Prefect execution tool.

## Inputs
- `search` *(string, optional)* – Case-insensitive filter on callable, deployment name, description, or tags.
- `include_tags` *(boolean, optional, default true)* – When false, omits tag arrays from the payload to reduce size.

## Outputs
- `flows` *(array)* – Each element includes:
  - `callable` *(string)* – Python path to the flow.
  - `deployment_name` *(string)* – Prefect deployment identifier (`<flow-name>/<deployment>`).
  - `description` *(string, optional)* – Human-friendly summary from the manifest.
  - `work_pool`, `work_queue` *(strings, optional)* – Deployment execution targets.
  - `tags` *(array, optional)* – Helpful for grouping flows by domain (`tool`, `neo4j`, `agent`, etc.).
- `instructions` *(string)* – Reminder to use the Prefect execution tool for actual runs.

## Usage Notes
- Combine with `tool_search_tools` or `tool_agent_catalog` to validate that a discovered capability has a deployable flow.
- Deployment names are immediately runnable via `prefect_flow_executor` (`deployment_name` parameter).
- After registering new flows (via `prefect_loader`), rerun this tool to verify the manifest is up to date.

## Related Tools & Flows
- `prefect_flow_executor` – Executes deployments returned by this catalog.
- `tool_manifest_fetch` – Retrieves documentation tied to each tool/flow entry.
- `tool_graph_capabilities_map` – Shows how these flows connect to agents and tools within Neo4j.

