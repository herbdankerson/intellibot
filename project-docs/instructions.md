

Goals: Agent Executor Script, Tool Executor Script tested working in new folder: freshbot. the bones of intellibot are good but the codebase has become cluttered, we're going to canabalize the project, ditch microsoft agent framework and mcp and put in this database centric agent and tool architecture, keep it all open ai compliant, most everything speaks that including gemini and qwen (now running in docker). set this up initially using qwen for testing but also test it for gemini using lite llm (successfully used in intellibot, lite llm provides failover api key management, keep that) 

tool executor - prefect tool executor, all tools/agents defined as prefect flows, api server is thinly disguesed prefect server/service

## Status Snapshot (2025-10-11)

- API container runs with the new `PYTHONPATH`; `/freshbot/flows/execute` now triggers the Prefect-backed Qwen chat tool to `COMPLETED` (results currently non-persistent, so `result_error` notes the missing state data).
- ParadeDB has the refreshed registry rows from `src/freshbot/registry/*.yaml`; Prefect deployments for agents/tools are applied and serviced by a running worker on `freshbot-process/freshbot-default`.
- Gateway/runtime/executor unit tests pass (`tests/test_freshbot_prefect_executor.py`, `tests/test_freshbot_gateway.py`, `tests/test_freshbot_runtime.py`, `tests/test_freshbot_ingestion.py`). FastAPI route tests + Gemini/document smoke flows remain TODO.

Agents:
	Main/Planner: this agent has access to:	pg_search scoped to the kb, a "search_tools" search scoped to tools table and a "search_agents" search scoped to agents table, is active flags must be true for agents using the tool or agent search to see tools or agents. this agent also has access to prefect tools through a set of tools based on teh prefect mcp server but customized for our use/schema. The agent plans and executes prefect flows for tool/agent flows to fulfill user requests. For a final response, it curates a data packet and prompts a rosponder agent
	Auditor: audits responses and data packets, makes sure requirements are met
	Responder:Takes curated data and writes a response, has limited scoped access to kb for pg search
	Tool User: uses assigned tools and sources to solve tasks
	Code Executor: Executes python code to solve problems, chains tools as plugins in a docker container, for complex problems requiring coding vs simple dag execution
	Custom Agent: takes in agent schema, executes custom agents, can be from blank or use existing agent as template/defaults with changes passed in json
	


project tasks in sequence:

0) create connectors for sources

Gateway pattern

- Every external call now routes through a gateway definition stored in ParadeDB. Connector rows include a JSON schema describing the request/response payload, plus any prompts or overrides required by that gateway. Runtime helpers in `src/freshbot/gateways/` enforce those schemas before issuing network calls so tools stay consistent with the database definition.
- Use the generic table loader (`python -m freshbot.devtools.table_loader`) to seed or update connector/tool/agent tables from YAML/JSON files. The loader validates rows against the live table schema and supports `--conflict` columns for idempotent upserts. Place reusable registries under `src/freshbot/registry/` and re-run the loader whenever you add or change definitions.

1) create agent and tool executors/runners

2) adapt intake pipeline to this framework, its agents are not makred active (for system use only), this pipeline is used to chunk and embedd everything on intake, from chat messages to web entries
Ingest/embedd peipeline
input: kb table, contents (the entry), other column entries - schema needs to be defined and met
output: inserts whole entry, chunks, embeddings, summaries, ner/mined data
1) is it .html or other web docs not handled by docling? --->> convert to .md if so
2) docling converter/chunker, ner
3) classifier - first chunk and file name to qwen, returns code, legal or general
4) qwen for chunk summaries
5) gemini 2.5 flash for document summary (2.5 specifically for its large context window)
6) run embeddings on chunks and summaries based on classifiers (general + sp)
7) emotions/other classifiers

Implementation note: the ingestion tasks now dispatch these LLM steps through the Freshbot gateway layer. `etl/tasks/model_clients.py` delegates to `src/freshbot/pipeline/ingestion.py`, which triggers the ParadeDB-registered tools (`tool_qwen_chat` for classification/emotion signals and `tool_gemini_chat` for summaries) via `freshbot.executors.invoke_tool`. This keeps prompts, routing models, and schema validation inside the database-driven registry instead of hard-coding LiteLLM adapters.

Flow executions should hop through the API server rather than importing Prefect modules directly. `freshbot.executors.prefect.execute_flow` now POSTs to `/freshbot/flows/execute` on the main API service (configurable via `FRESHBOT_API_BASE_URL`). The FastAPI handler calls `prefect.deployments.run_deployment` so all tooling—including registry loaders and tests—hit the same surface.

kb table schema:
	include boolean, is searchable
	include boolean, is note
	include boolean is document, is website, etc these will have separate config tables
	include uuid if not already

bot notes meta table schema - all bots can access this for notes, comes with basic tools for looking up (optionally scoped) notes, notes link to uuid of the entry containing the note and by ref ids, the actual note is a kb entry
	session id
	chat id 
	uuid
	model
	time/date
	reference id(s) (chunk id for example or document id, a uuid in the kb) can be left blank for general notes, all agents contain a system prompt that references this and agents should be passed session and chat ids to search for notes and other info by them, these should be ran through the intake pipeline so they're chunked and embedded
	note

Agent Schema:
	Model: default Qwen 3
	Special Agent File: used for implementing scripted agent behavior, can be a prefect run or thinking method like tree of thoughts, anything that defines special agent behaviors, none by default, stored in agents dir 
	Settings: model specific settings, stored in postgres models table, default is provider defaults, json in db
	Prompt: none default, uuid or row id of prompt from postgres prompt table
	Tools: none default, uuid or row id of tools or toolboxes assigned, json in db in agents table
	Sources: Access to sources, like postgres, neo4j or searxng. Each source accepts a scope and can be scoped for each role, scopes must be defined if none then default is open or full scope
	is active
Tool Schema:
	Sources: none by default, sources for the tool to interact with, scoped to agent domain or can be custom set in the executable, scope must be passed at the source, a source must be added to an agent for a tool requiring it to work
	Executable File: name of the file to run in /tools folder, the tool entry point
	input schema: must be defined, json
	ouptut schema: must be defined, json
	descripton: what does the tool do, what does it access, these will be vector searchable and must be descriptive
	is active: boolean
Tool Box Schema:
	json array of tools
	is active: boolean
	description: required, vectored

many tables already exist, please pull from the database for most current ones, check for a list of all tables, they may be named different but lets not clutter...owui has its base tables also, any time we can share those great but we can do some workarounds for now and we can do those later, right now I just want to get some basics in, break these tasks down into phases and stop in between each phase, get any info from me you need for clarification between each phase and just in general check in, update on the project. a large number of phases is fine, keep things small. all agents, tools, toolboxes etc are stored in the database in tables/regestries, so all we really need are the executor files and some test scripts to verify the pipeline works, use a qwen and have it call a tool etc, test the ingestion pipeline all  of that, but start the project in the sequence described above, incorporating everything noted in a fresh project, keep things light and modular, database centric

	


## Freshbot Executor Architecture (Implemented)

- **Tools**: Prefect flows live under `src/freshbot/flows/tools.py` and are registered through `src/freshbot/registry/tools.yaml`. Each tool record stores the callable path, input/output schema hints, and defaults such as the gateway alias. The generic runtime helper `freshbot.executors.runtime.invoke_tool()` loads these entries, merges agent-specific overrides from `cfg.agent_tools`, and executes the flow (either directly or via `freshbot.executors.prefect.execute_flow`).
- **Agents**: Agent flows reside in `src/freshbot/flows/agents.py` and are referenced in `cfg.agents.params.entrypoint`. Planner, tool-user, responder, auditor, code-executor, and custom agents all route through the same runtime (`invoke_agent`) so the API layer can trigger them by name. System prompts are stored in `cfg.prompts` and linked via `params.prompt_ref`.
- **Deployments**: All agent and tool flows are registered in `src/freshbot/flows/flows.yaml`. Apply them with `python -m freshbot.devtools.prefect_loader --flows-spec src/freshbot/flows/flows.yaml --apply` from the `api` container. This seeds deployments such as `freshbot-agent-planner` and `freshbot-tool-qwen-chat` onto the `freshbot-process/freshbot-default` work pool/queue.
- **Registry loading**: Run `python -m freshbot.devtools.registry_loader --registry-dir src/freshbot/registry --apply` to sync providers/models/tools/agents/prompts. The loader is idempotent, so updates to schemas or overrides can be reapplied safely.
- **Runtime usage**: The API (or tests) can call `from freshbot.executors import invoke_agent, invoke_tool` and pass payloads. Returned `InvocationResult` objects include both the flow result and any metadata (input/output schemas, descriptions) declared in the registry rows.

Refer to `project-docs/freshbot_phase0.md` for loader/Prefect CLI commands and to `project-docs/ingestion-flow.md` for KB validation steps.
