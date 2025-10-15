# Tool: `prefect_flow_executor`

## Overview
- Generic entry point for launching Prefect deployments via the Freshbot API (`freshbot.executors.prefect:execute_flow`).
- Powers the “execute” step in the modular architecture—anything discoverable in `flows.yaml` can be triggered through this tool.
- Handles synchronous or asynchronous execution (`wait_for_completion` flag).

## Inputs
- `deployment_name` *(string, required)* – Prefect deployment identifier (`<flow-name>/<deployment>`).
- `parameters` *(object, optional, default `{}`)* – JSON payload passed to the flow.
- `wait_for_completion` *(boolean, optional, default false)* – Wait for terminal state when true; otherwise returns scheduling metadata.

## Outputs
- `flow_run_id` *(string)* – Prefect flow run ID created for the deployment.
- `state_name` *(string)* – Reported state at completion (or scheduled state when not waiting).
- `state_type` *(string, optional)* – Prefect state type (`COMPLETED`, `FAILED`, etc.).
- `created` *(string, optional)* – Timestamp from Prefect response when available.
- Any additional metadata provided by the Prefect API.

## Usage Notes
- Always validate deployments with `tool_prefect_flow_catalog` before execution; the tool ensures names are up to date.
- Provide parameters as plain JSON; ensure serialisable values (no complex Python objects).
- When `wait_for_completion=false`, poll Prefect separately (`prefect flow-run ls`) or rerun with the same `flow_run_id`.
- Errors from Prefect bubble up as exceptions—surface them in activity logs or mark tasks blocked.

## Related Tools & Flows
- `tool_prefect_flow_catalog` – Discover deployment names and descriptions.
- `tool_manifest_fetch` – Retrieve schema documentation before invoking a tool flow.
- `tool_graph_capabilities_map` – Confirm downstream graph updates after executing ingestion or sync flows.

