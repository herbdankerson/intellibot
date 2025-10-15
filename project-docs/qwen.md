# Qwen3 Usage Guide

## Service Overview
- The `qwen3` container in `docker-compose.yml` exposes the OpenAI-compatible HTTP API on `localhost:8001` and mounts a persistent model cache volume; see `docker-compose.yml:146` and `docker-compose.yml:162`.
- `qwen3-coder` mirrors the same server image on `localhost:8002` but binds in the downloaded `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`; the alias and CPU-only load guard are exported via `LLAMA_ARG_MODEL`/`LLAMA_ARG_ALIAS`/`LLAMA_ARG_N_GPU_LAYERS` (`docker-compose.yml:178`-`docker-compose.yml:202`).

## Starting the Container
- From the repository root run `docker compose up qwen3` to boot only the 14B model service.
- `docker compose up qwen3-coder` spins up the 30B variant once `models/qwen3-coder/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` is present; the GGUF mount is read-only and the deployment defaults to CPU layers unless you raise the GPU layer count (`docker-compose.yml:193`-`docker-compose.yml:201`).

## Health and Model Discovery
- Verify each container with `curl http://127.0.0.1:8001/health` (14B) or `curl http://127.0.0.1:8002/health` (30B).
- Optional: set `QWEN3_API_BASE` or `QWEN3_MODEL_ID` to point the test suite at either deployment (`tests/test_qwen3_tool_call.py:12`-`tests/test_qwen3_tool_call.py:26`).
- If `QWEN3_MODEL_ID` is unset, the helper logic queries `/v1/models` and uses the first available identifier; `qwen3` reports the downloaded 14B id while `qwen3-coder` advertises `qwen3-coder-30b-a3b-q4km` (`tests/test_qwen3_tool_call.py:24`-`tests/test_qwen3_tool_call.py:36`).

## Tool-Calling Workflow
- The regression test at `tests/test_qwen3_tool_call.py:40`-`tests/test_qwen3_tool_call.py:98` shows a minimal payload that makes Qwen3 emit an OpenAI-format tool call.
- Key request fields:
  - Tool definition for `searxngsearch` with JSON schema describing `query` and optional `language`.
  - `reasoning_format="deepseek-legacy"` and `thinking_forced_open=True` to expose the model's chain-of-thought channel.
  - `parse_tool_calls=True` so the server returns structured arguments.
- To replay the scenario manually, send the JSON payload from that test to `/v1/chat/completions` and confirm the first `tool_calls[0]` targets `searxngsearch` with a lowercase or title-case "CHIPS Act" query.

## Reasoning Mode with Evidence
- `tests/test_qwen3_reasoning.py:77`-`tests/test_qwen3_reasoning.py:139` drives a full reasoning call that cites evidence sourced via SearxNG.
- The helper `_collect_searx_evidence` fetches JSON results from the MCP search service (`tests/test_qwen3_reasoning.py:43`-`tests/test_qwen3_reasoning.py:74`) and formats them into numbered snippets included in the user message.
- The system prompt constrains the answer to three sentences, requires inline `[n]` citations, and appends a `Sources:` list (`tests/test_qwen3_reasoning.py:99`-`tests/test_qwen3_reasoning.py:107`).
- Successful responses include both `message["reasoning_content"]` and a final answer that references the first evidence item (assertions at `tests/test_qwen3_reasoning.py:131`-`tests/test_qwen3_reasoning.py:136`).

## Local Validation
- Slow integration tests are enabled via the pytest marker registered in `pyproject.toml:34`-`pyproject.toml:39`.
- Run `pytest tests/test_qwen3_tool_call.py -vv -m slow` to confirm tool-call behavior and `pytest tests/test_qwen3_reasoning.py -vv -m slow` to exercise the reasoning flow. Point `QWEN3_API_BASE` at `http://127.0.0.1:8002` to validate the coder model; both tests require the target container (and the SearxNG MCP service if you want real evidence) to be online.
- For quick smoke tests without external services, you can stub the SearxNG endpoint; the reasoning test skips automatically when the search server is absent (`tests/test_qwen3_reasoning.py:55`-`tests/test_qwen3_reasoning.py:74`).
