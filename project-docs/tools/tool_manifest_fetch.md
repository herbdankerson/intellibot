# Tool: `tool_manifest_fetch`

## Overview
- Returns the authoritative registry entry for a tool (`cfg.tools`) plus any linked documentation stored in the knowledge base.
- Designed as the primary “help” surface: planners can retrieve schema hints, manifest references, and README UUIDs in one call.
- Supports iterating quickly between discovery and execution while keeping instructions centralised.

## Inputs
- `tool_slug` *(string, required)* – Identifier recorded in `cfg.tools.slug`.
- `include_docs` *(boolean, optional, default true)* – Include associated documentation metadata when true.

## Outputs
- `tool` *(object)* – Registry entry containing `slug`, `kind`, `manifest_or_ref`, `default_params`, `enabled`, and `notes`.
- `documents` *(array, optional)* – Each doc provides `document_id`, `file_name`, `summary`, `version`, `updated_at`, and raw metadata.
- `next_steps` *(string)* – Guidance on invoking the tool via Prefect or reviewing linked docs.

## Usage Notes
- Use immediately after `tool_search_tools` to confirm how to call a given tool.
- When `documents` is non-empty, pass the returned UUIDs into `get_document` or KB search to obtain full instructions.
- Registry-driven systems keep tool schemas in `default_params`; rely on them to parameterise agent prompts or validation checks.
- Documentation linkage depends on ingestion metadata (`metadata.extra.tool_slugs`); ensure new tool docs include the slug.

## Related Tools & Flows
- `tool_search_tools` – Discover tools before fetching manifests.
- `prefect_flow_executor` – Execute flows referenced by `manifest_or_ref` values pointing at Prefect deployments.
- `tool_graph_capabilities_map` – Validates graph relationships for the returned tool slug.

