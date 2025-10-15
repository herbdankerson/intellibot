# Tool: `tool_search_agents`

## Overview
- Lists agent definitions stored in `cfg.agents`, filtered by name, type, or descriptive notes.
- Supports selecting thinking strategies (ReAct, sequential, RAP, etc.) without redeploying code.
- Complements `tool_agent_catalog` by providing fast search during early planning steps.

## Inputs
- `query` *(string, required)* – Text fragment matched against agent name, type, and notes.
- `limit` *(integer, optional, default 10)* – Maximum number of rows to return.
- `include_disabled` *(boolean, optional, default false)* – Include disabled agents for auditing when true.

## Outputs
- `results` *(array)* – Each entry contains:
  - `name` *(string)* – Agent slug.
  - `type` *(string)* – High-level agent category (`planner`, `responder`, etc.).
  - `model_alias` *(string, optional)* – Default model alias resolved via `cfg.models`.
  - `params` *(object)* – JSON parameters (strategy hints, overrides, system prompt refs).
  - `enabled` *(boolean)* – Whether the agent is available.
  - `notes` *(string, optional)* – Operational tips or limitations.

## Usage Notes
- Once an agent is selected, call `tool_agent_catalog` to see the full configuration, tool bindings, and thinking mode.
- Pair the chosen agent with `prefect_flow_executor` to launch the associated Prefect flow (`freshbot-agent-planner`, etc.).
- Update agent metadata via scripted registry loaders rather than editing code—changes propagate instantly.

## Related Tools & Flows
- `tool_agent_catalog` – Detailed agent definitions, tool bindings, and instructions.
- `tool_prefect_flow_catalog` – Deployment lookup for triggering agent flows.
- `tool_graph_capabilities_map` – Confirms the agent-to-tool relationships stored in Neo4j.

