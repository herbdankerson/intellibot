# Tool: `tool_graph_capabilities_map`

## Overview
- Reads Neo4j to surface relationships between tools, agents, and flows (captured by the task/graph sync pipeline).
- Designed to keep graph awareness top-of-mind: planners should check it first when orchestrating complex DAGs.
- Provides both relationship edges and optional node listings for quick visualisation or downstream rendering.

## Inputs
- `limit` *(integer, optional, default 100)* – Maximum rows to return for edges and nodes.
- `include_nodes` *(boolean, optional, default true)* – Include node snapshots when true.

## Outputs
- `relationships` *(array)* – Each element includes:
  - `tool_slug` *(string)* – Source tool node.
  - `target_labels` *(array)* – Labels assigned to the target node (e.g., `Agent`, `Flow`).
  - `target_id` *(string)* – Identifier for the target node (slug/name/UUID).
  - `relationship` *(string)* – Neo4j relationship type.
- `nodes` *(array, optional)* – When requested, returns label + identifier + description for key nodes.
- `available` *(boolean)* – Indicates whether the Neo4j driver was reachable.
- `instructions` *(string)* – Tips on using Neo4j tools or Prefect sync flows to extend the graph.

## Usage Notes
- If `available` is false, ensure the `neo4j` Python package is installed and the `NEO4J_*` env vars are set.
- Use alongside `tool_manifest_fetch` and `tool_agent_catalog` to verify consistency between registry state and graph state.
- After uploading new DAGs or editing tool docs, rerun `task-sync-neo4j/freshbot-task-graph-sync` then call this tool to confirm the graph.
- The `target_labels` array helps determine whether a tool primarily interacts with agents, flows, or other tool nodes.

## Related Tools & Flows
- `task-sync-neo4j/freshbot-task-graph-sync` Prefect deployment – Refreshes the Neo4j mirror.
- `tool_search_tools` / `tool_search_agents` – Provide the slugs referenced by the relationships.
- `tool_prefect_flow_catalog` – Maps graph nodes back to Prefect deployments for live execution.

