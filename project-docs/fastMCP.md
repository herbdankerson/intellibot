Perfect—here’s a **complete, generic, copy-pasteable starter kit** for your “plugin-style, HTTP FastMCP host,” with **placeholders/stubs** so you can drop it into your IDE and fill in the blanks later. It uses only **official FastMCP patterns** (`@mcp.tool`, `@mcp.resource`, `mount()`/`import_server()`, HTTP transport). Two modes included:

* **Mode A (DB-driven):** plugin registry in Postgres, code in files.
* **Mode B (File-list):** no DB, just import a list of plugin modules.

Use whichever you want today; you can keep both side-by-side.

---

# 0) Requirements

```bash
# runtime
pip install fastmcp httpx

# if you use Mode A (DB-driven)
pip install asyncpg python-dotenv

# optional (dev convenience)
pip install uv
```

---

# 1) Project layout (placeholders marked with ⟪…⟫)

```
fastmcp_host/
├─ .env.example
├─ README.md
├─ pyproject.toml                  # or requirements.txt; your call
├─ sql/
│  └─ 001_registry.sql            # Mode A: Postgres registry schema
├─ host_files.py                   # Mode B: file-list host (no DB)
├─ host_db.py                      # Mode A: DB-driven host (registry in Postgres)
├─ plugins/
│  ├─ plugin_files.py              # example plugin with tools/resources
│  ├─ plugin_web.py                # example plugin with async tool
│  └─ plugin_help.py               # stub "help/chooser" plugin (you can wire Neo4j/embeddings later)
```

---

# 2) `.env.example` (copy to `.env` and fill in)

```dotenv
# Common HTTP server settings
MCP_HTTP_HOST=0.0.0.0
MCP_HTTP_PORT=⟪8000⟫
MCP_HTTP_PATH=/mcp

# Mode A: Postgres (only if you use host_db.py)
PG_DSN=postgresql://⟪user⟫:⟪password⟫@⟪host⟫:⟪5432⟫/⟪database⟫

# Optional: simple API key auth via reverse proxy or your own middleware later
MCP_API_KEY=⟪set-if-you-offload-auth⟫
```

*(This starter doesn’t wire HTTP auth; add it later if you expose beyond localhost.)*

---

# 3) Mode A (DB-driven registry): `sql/001_registry.sql`

```sql
-- schema + tables for a plugin registry; keep code in files, toggles in DB
create schema if not exists registry;

create table if not exists registry.mcp_plugins (
  id bigserial primary key,
  module_path text not null,         -- e.g. 'plugins.plugin_files'
  prefix text,                       -- e.g. 'files'
  enabled boolean not null default true,
  load_mode text not null default 'mount',  -- 'mount' or 'import'
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists mcp_plugins_enabled_idx on registry.mcp_plugins (enabled);

create table if not exists registry.mcp_tool_docs (
  id bigserial primary key,
  module_path text not null,         -- expected plugin module
  tool_name text not null,           -- final MCP tool name (after prefix)
  short_desc text,
  instructions text,                 -- long-form docs; embed these later
  tags text[],
  updated_at timestamptz not null default now()
);

-- seed rows (edit to your modules/prefixes)
insert into registry.mcp_plugins (module_path, prefix, enabled, load_mode, notes)
values
 ('plugins.plugin_files', 'files', true, 'mount', 'File I/O tools'),
 ('plugins.plugin_web',   'web',   true, 'mount', 'HTTP fetch tools'),
 ('plugins.plugin_help',  'help',  true, 'import', 'Help/chooser tools');

-- optional docs seed (adjust as needed)
insert into registry.mcp_tool_docs (module_path, tool_name, short_desc, instructions, tags)
values
 ('plugins.plugin_files', 'files_read_file', 'Read text file',
  'Reads small UTF-8 text files. For large files, prefer ranged reads or summaries.', '{files,io}'),
 ('plugins.plugin_web',   'web_fetch', 'Fetch URL',
  'Fetches a URL with timeout and returns status + preview. Use only http(s) URLs.', '{http,fetch}');
```

---

# 4) Mode A host (DB-driven): `host_db.py`

```python
# host_db.py
# Purpose: one HTTP FastMCP endpoint that composes multiple plugins declared in Postgres.
# Only official FastMCP APIs: FastMCP(), @mcp.tool, mount()/import_server(), run_async(..., transport="http")

import os, asyncio, importlib
from dataclasses import dataclass
from typing import Optional, Literal

import asyncpg
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

PG_DSN = os.getenv("PG_DSN", "postgresql://⟪user⟫:⟪pass⟫@⟪host⟫:5432/⟪db⟫")
HTTP_HOST = os.getenv("MCP_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("MCP_HTTP_PORT", "8000"))
HTTP_PATH = os.getenv("MCP_HTTP_PATH", "/mcp")

@dataclass
class PluginRow:
    module_path: str
    prefix: Optional[str]
    load_mode: Literal["mount","import"]

SQL_FETCH = """
select module_path, prefix, load_mode
from registry.mcp_plugins
where enabled = true
order by id;
"""

async def fetch_plugins() -> list[PluginRow]:
    conn = await asyncpg.connect(PG_DSN)
    try:
        rows = await conn.fetch(SQL_FETCH)
        out: list[PluginRow] = []
        for r in rows:
            mode = r["load_mode"] if r["load_mode"] in ("mount","import") else "mount"
            out.append(PluginRow(
                module_path=r["module_path"],
                prefix=r["prefix"],
                load_mode=mode
            ))
        return out
    finally:
        await conn.close()

async def build_host() -> FastMCP:
    host = FastMCP(name="⟪YourHostName⟫")

    for p in await fetch_plugins():
        mod = importlib.import_module(p.module_path)

        # Convention: each plugin module exposes a FastMCP instance named "*_mcp"
        # e.g., plugins/plugin_files.py -> files_mcp
        mcp_obj = next(
            (getattr(mod, attr) for attr in dir(mod) if attr.endswith("_mcp") and hasattr(getattr(mod, attr), "tool")),
            None
        )
        if mcp_obj is None:
            raise RuntimeError(f"Plugin {p.module_path} must expose a FastMCP instance variable ending with '_mcp'")

        if p.load_mode == "import":
            await host.import_server(mcp_obj, prefix=p.prefix)
        else:
            await host.mount(mcp_obj, prefix=p.prefix)

    # Optional health route (served by same HTTP server)
    @host.custom_route("/health", methods=["GET"])
    async def health(_request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("OK")

    return host

async def main():
    host = await build_host()
    await host.run_async(transport="http", host=HTTP_HOST, port=HTTP_PORT, path=HTTP_PATH)

if __name__ == "__main__":
    asyncio.run(main())
```

**Run (Mode A):**

```bash
psql ⟪PG_DSN⟫ -f sql/001_registry.sql
python host_db.py
# http://localhost:⟪8000⟫/mcp
```

---

# 5) Mode B host (file-list): `host_files.py`

```python
# host_files.py
# Purpose: simpler host; no DB. Edit PLUGINS to add/remove modules.

import os, asyncio, importlib
from dataclasses import dataclass
from typing import Optional, Literal

from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()
HTTP_HOST = os.getenv("MCP_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("MCP_HTTP_PORT", "8000"))
HTTP_PATH = os.getenv("MCP_HTTP_PATH", "/mcp")

@dataclass
class PluginSpec:
    module_path: str            # 'plugins.plugin_files'
    prefix: Optional[str] = None
    load_mode: Literal["mount","import"] = "mount"

PLUGINS: list[PluginSpec] = [
    PluginSpec("plugins.plugin_files", prefix="files", load_mode="mount"),
    PluginSpec("plugins.plugin_web",   prefix="web",   load_mode="mount"),
    PluginSpec("plugins.plugin_help",  prefix="help",  load_mode="import"),
]

async def build_host() -> FastMCP:
    host = FastMCP(name="⟪YourHostName⟫")

    for p in PLUGINS:
        mod = importlib.import_module(p.module_path)
        mcp_obj = next(
            (getattr(mod, attr) for attr in dir(mod) if attr.endswith("_mcp") and hasattr(getattr(mod, attr), "tool")),
            None
        )
        if mcp_obj is None:
            raise RuntimeError(f"Plugin {p.module_path} must expose a FastMCP instance variable ending with '_mcp'")

        if p.load_mode == "import":
            await host.import_server(mcp_obj, prefix=p.prefix)
        else:
            await host.mount(mcp_obj, prefix=p.prefix)

    @host.custom_route("/health", methods=["GET"])
    async def health(_request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("OK")

    return host

async def main():
    host = await build_host()
    await host.run_async(transport="http", host=HTTP_HOST, port=HTTP_PORT, path=HTTP_PATH)

if __name__ == "__main__":
    asyncio.run(main())
```

**Run (Mode B):**

```bash
python host_files.py
# http://localhost:⟪8000⟫/mcp
```

---

# 6) Example plugin A: files — `plugins/plugin_files.py`

```python
# plugins/plugin_files.py
from fastmcp import FastMCP

files_mcp = FastMCP(name="Files")

@files_mcp.tool(description="Read a small UTF-8 text file and return its content.")
def read_file(path: str) -> str:
    # TODO: ⟪add path safelist or sandbox if exposing beyond localhost⟫
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

@files_mcp.tool(description="Write text to a UTF-8 file (overwrites).")
def write_file(path: str, content: str) -> str:
    # TODO: ⟪add path safelist or sandbox⟫
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return "ok"

@files_mcp.resource("data://files/notes")
def sample_notes() -> str:
    """Example resource; replace with something useful later."""
    return "These are placeholder notes."
```

---

# 7) Example plugin B: web — `plugins/plugin_web.py`

```python
# plugins/plugin_web.py
from fastmcp import FastMCP
import httpx

web_mcp = FastMCP(name="Web")

@web_mcp.tool(description="Fetch a URL; returns status and a short preview.")
async def fetch(url: str, timeout_secs: float = 20.0, preview_chars: int = 1000) -> dict:
    # TODO: ⟪validate scheme http/https, block local/metadata IPs if exposed externally⟫
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_secs) as client:
        r = await client.get(url)
    text = r.text or ""
    return {"status": r.status_code, "preview": text[:preview_chars]}
```

---

# 8) Example plugin C: help/chooser (stub) — `plugins/plugin_help.py`

```python
# plugins/plugin_help.py
# Stub "help" tools you can later connect to Neo4j/pgvector/Qdrant.
# For now it just echoes back candidates with placeholder metadata.

from fastmcp import FastMCP

help_mcp = FastMCP(name="Help")

@help_mcp.tool(description="Suggest likely tools for a user task (stub).")
def suggest_tools(task: str, limit: int = 5) -> dict:
    """
    TODO:
      - call your embeddings store to retrieve relevant tool docs
      - query graph for capabilities/dependencies
      - return ranked tool names + suggested args
    """
    # Placeholder response shape:
    return {
        "task": task,
        "candidates": [
            {"tool": "files_read_file", "why": "task mentions 'open/read file'"},
            {"tool": "web_fetch", "why": "task mentions 'download/fetch URL'"}
        ][:max(1, limit)]
    }
```

*(Mounting with prefix `help` makes this appear as `help_suggest_tools`.)*

---

# 9) pyproject.toml (optional)

```toml
[project]
name = "fastmcp-host"
version = "0.0.1"
requires-python = ">=3.10"
dependencies = [
  "fastmcp",
  "httpx",
  "python-dotenv",
  "asyncpg",
]
```

---

# 10) Run + test quickly

```bash
# Mode B first (no DB)
cp .env.example .env
python host_files.py

# In your client (Claude Desktop, Cursor, GPT MCP config):
#   "mcpServers": { "plugin-host": { "url": "http://127.0.0.1:⟪8000⟫/mcp" } }

# Try listing tools; you should see:
#   files_read_file, files_write_file, web_fetch, help_suggest_tools
```

---

# 11) Ops notes (brief, no fluff)

* **HTTP over SSE:** stick with `transport="http"`; SSE is legacy.
* **`mount` vs `import_server`:**

  * `mount()` = live link; nice for dev; a tad more overhead.
  * `import_server()` = static copy; faster; restart to pick up changes.
* **Security:** if exposing beyond localhost, add path safelists for file ops, URL allow-lists, and real auth (API key/OAuth) at the proxy or FastMCP auth providers.
* **Hot reload:** simplest is to restart the host when DB rows change; hot-swapping mid-session can be messy.
* **Docs/KB:** keep **executable code in files**; store docs/prompts/flags in DB; index those docs in your vector store/graph for the help tool.

---

If you want, tell me your actual module paths and desired prefixes; I’ll output the exact `host_db.py` seed rows and a ready-to-run command set for your stack (including your Postgres schema names).
