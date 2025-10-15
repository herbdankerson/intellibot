# Tool: `tool_agent_catalog`

## Overview
- Aggregates `cfg.agents` and `cfg.agent_tools` to provide a detailed view of each agent: thinking mode, default params, tool bindings, and database scopes.
- Complements quick searches by giving planners everything needed to launch or clone an agent configuration.
- Outputs are structured for downstream serialisation (e.g., storing snapshots, feeding planners, or building UI dropdowns).

## Inputs
- `include_disabled` *(boolean, optional, default false)* – Include disabled agents when true.

## Outputs
- `agents` *(array)* – Each entry contains:
  - `name` *(string)* – Agent slug used across the stack.
  - `type` *(string)* – Role classification (`planner`, `responder`, `auditor`, etc.).
  - `model_alias` *(string, optional)* – Default runtime model.
  - `thinking_mode` *(string, optional)* – Pulled from agent params (`strategy` / `thinking_mode` keys).
  - `default_params` *(object)* – Raw JSON parameters stored in the registry.
  - `db_scope` *(array)* – Databases / schemas the agent is allowed to read.
  - `enabled` *(boolean)* – Current availability.
  - `notes` *(string, optional)* – Additional operator comments.
  - `tools` *(array)* – Tool bindings with per-agent overrides.
- `instructions` *(string)* – Steps for invoking Prefect agent flows using this information.

## Usage Notes
- After selecting an agent, call `prefect_flow_executor` with the associated deployment (e.g., `freshbot-agent-planner`).
- Use `tools` overrides when constructing agent requests—these indicate default parameters or restrictions.
- To clone or modify an agent, copy the `default_params`, adjust strategy values, and push via the registry loader.
- Combine with `tool_graph_capabilities_map` to ensure graph provenance is updated when agents change.

## Related Tools & Flows
- `tool_search_agents` – Discover agent slugs before requesting the full catalog.
- `tool_prefect_flow_catalog` – Confirm the deployment name for the chosen agent.
- `tool_manifest_fetch` – Pull documentation for tools bound to each agent.

