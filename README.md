# TriageAgent

An IT ticket triage agent: it classifies an incident, retrieves relevant KB articles, proposes an action, waits for **human approval**, then executes it through **MCP tools** against a ServiceNow-shaped API.

> **Status: Phases 0–2 done** (see [Roadmap](#roadmap)): mock ServiceNow API, client, MCP server and the LangGraph agent with human approval, runnable from the CLI. RAG, guardrails and evals are next. Eval results and a live link will be added once they exist.

## Architecture (target)

```mermaid
flowchart LR
    T[Incident] --> G{guardrails}
    G -->|injection detected| S[summarize]
    G --> C[classify<br/>category, impact, urgency, group]
    C --> R[retrieve KB<br/>vector store + citations]
    R --> P[propose action<br/>Pydantic, tool allowlist]
    P --> A{{human approval<br/>interrupt + SQLite checkpointer}}
    A -->|approve| E[execute]
    A -->|reject| S
    E --> S
    E -- MCP --> M[MCP server<br/>search_kb, get/create/update_incident, assign_group]
    M -- REST: auth, retries, timeouts --> API[(mock ServiceNow<br/>FastAPI + SQLite)]
```

Every LLM step returns a validated Pydantic object. The LLM never calls write tools directly: it proposes, code validates, a human approves, and only then does the execute node call the tools.

## Quickstart (Windows PowerShell)

```powershell
# One-time: install uv  (https://docs.astral.sh/uv/getting-started/installation/)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

cd D:\TriageAgent
git init
copy .env.example .env
uv sync                      # creates .venv; downloads Python 3.12 if you don't have it
uv run mock-snow             # mock API on http://127.0.0.1:8080  (Swagger UI at /docs)
```

In a second terminal:

```powershell
uv run pytest                                  # infrastructure + contract tests pass; the rest need the TODOs
uv run triage run INC0010001                   # triage one incident (needs LLM_MODEL + GEMINI_API_KEY in .env)
uv run python examples/hitl_demo.py            # interrupt/resume concept check (no LLM, no keys)

# Untriaged incidents (use curl.exe in PowerShell, not the curl alias)
curl.exe -u triage.agent:change-me "http://127.0.0.1:8080/api/now/table/incident?sysparm_query=active=true^categoryISEMPTY&sysparm_fields=number,short_description&sysparm_display_value=true"
```

## Layout

```
mock_snow/      Mock ServiceNow Table API (FastAPI + SQLite), seeded with groups, users, incidents
triage/
  config.py       Settings from env / .env
  snow_client.py  Async Table API client: auth, retries, timeouts, idempotent create
  mcp_server/     MCP server (SDK v2): tools, resource, contracts
  llm.py          Gemini / Azure OpenAI swap; call_structured (validation, one repair retry, usage)
  agent/          runtime.py (MCP gateway + allowlist), state.py, schemas.py (LLM outputs),
                  prompts.py, nodes.py (the steps), graph.py (the wiring)
  cli.py          triage run / resume / show
examples/       hitl_demo.py: LangGraph interrupt + persistent checkpointer, no LLM
tests/          Specs per layer; fakes.py has a scripted LLM so tests never call a real model
```

## Mock ServiceNow API

This is a small subset shaped like the real ServiceNow Table API (envelopes, field names, `sysparm_*` params, string values, reference links). It is **not** ServiceNow.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/now/table/{table}` | `sysparm_query`, `sysparm_limit` (≤1000), `sysparm_offset`, `sysparm_fields`, `sysparm_display_value` (`true`/`false`/`all`), `sysparm_exclude_reference_link`; `X-Total-Count` header |
| GET | `/api/now/table/{table}/{sys_id}` | 404 → `No Record found` |
| POST | `/api/now/table/{table}` | 201 + `Location` |
| PATCH / PUT | `/api/now/table/{table}/{sys_id}` | both are partial updates, as in the Table API |
| DELETE | `/api/now/table/{table}/{sys_id}` | 204 |
| GET | `/health` | no auth |

**Tables:** `incident` (read/write), and `sys_user_group`, `sys_user` and `sys_journal_field` (read-only: the integration user has least-privilege ACLs).
**Auth:** Basic (`SNOW_USERNAME`/`SNOW_PASSWORD`) or Bearer (`SNOW_TOKEN`). Errors use the ServiceNow envelope: `{"error": {"message", "detail"}, "status": "failure"}`.
**Encoded queries:** `=`, `!=`, `<`, `<=`, `>`, `>=`, `LIKE`, `NOT LIKE`, `STARTSWITH`, `ENDSWITH`, `IN`, `NOT IN`, `ISEMPTY`, `ISNOTEMPTY`, `ORDERBY`/`ORDERBYDESC`, joined with `^`. `^OR`, `^NQ` and dot-walking are not supported.

**Business rules on `incident`**

- `number` is auto-generated (`INC0010009`, …).
- `priority` is **derived** from impact × urgency (1+1 → 1 Critical … 3+3 → 5 Planning). Writing it directly is rejected, so the agent classifies impact and urgency, not priority.
- Moving to Resolved (6) or Closed (7) requires `close_code` and `close_notes`. `active` follows state.
- `work_notes` and `comments` are journal fields: writes are stored as `sys_journal_field` rows and read back as `""`, like the real API.
- `correlation_id` is the hook for idempotent creates (see `triage/snow_client.py`).

**Deliberate deviations (strict on purpose).** Real ServiceNow often silently ignores bad input. This mock returns 400 for unknown fields, read-only fields, invalid choice values, dangling references and unknown query fields, so an agent's tool errors surface instead of disappearing.

### Fault injection (`/_mock/*`, not part of ServiceNow)

Used to test retries, timeouts and idempotency for real. Disable on public deploys with `SNOW_MOCK_ADMIN=false`.

```powershell
$mock = "http://127.0.0.1:8080/_mock"
# Next 2 requests fail with 503 before touching the DB (safe to retry)
irm -Method Post "$mock/faults" -ContentType application/json -Body '{"status": 503, "count": 2}'
# Next POST is written, THEN answered with 503: a lost response
irm -Method Post "$mock/faults" -ContentType application/json -Body '{"status": 503, "when": "after", "method": "POST"}'
irm "$mock/requests"                 # request log (for counting attempts)
irm -Method Post "$mock/reset"       # re-seed DB, clear faults + log
```

`FaultSpec` fields: `status` (or `null` for latency only), `count`, `when` (`before`/`after`), `delay_ms`, `retry_after`, `method`, `path_contains`. `SNOW_MOCK_FAULT_RATE=0.1` turns on random 503s for chaos testing.

## MCP server

Built on MCP Python SDK v2 (`MCPServer`, spec 2026-07-28; legacy clients that use the older initialize handshake are still accepted). It runs over stdio and needs the mock API to be running.

| Tool | Annotations | Notes |
|---|---|---|
| `get_incident(number)` | read-only | references resolved to names |
| `create_incident(short_description, description, correlation_id, caller_user_name?)` | idempotent | same `correlation_id` → same incident |
| `update_incident(number, category?, impact?, urgency?, state?, close_code?, close_notes?, work_note?)` | destructive | no `priority` argument: it's derived |
| `assign_group(number, group, work_note?)` | destructive | group by name; unknown names fail with the valid list |

Resource: `snow://assignment-groups` (JSON list of active groups).

All tools return a typed `IncidentView` (structured output). Failures come back as `is_error` results with messages the model can act on, and never include URLs, credentials or stack traces. Annotations are hints for hosts, not a security control. The approval gate in the agent is the security control.

```powershell
uv run python -m triage.mcp_server                                   # what a host launches (stdio)
npx @modelcontextprotocol/inspector uv run python -m triage.mcp_server   # poke it in MCP Inspector (not tested here)
```

**Use it from Claude Desktop** (`claude_desktop_config.json`). Note that this lets the model call write tools directly, without the agent's approval gate:

```json
{
  "mcpServers": {
    "triage-snow": {
      "command": "uv",
      "args": ["--directory", "D:\\TriageAgent", "run", "python", "-m", "triage.mcp_server"]
    }
  }
}
```

## Agent CLI

```powershell
uv run triage run INC0010001                       # classify -> propose -> asks you to approve
uv run triage run INC0010001 --no-prompt           # stop at the approval point (state is checkpointed)
uv run triage resume INC0010001-a1b2c3 approve --comment "ok"
uv run triage show INC0010001-a1b2c3               # state of any run
```

Runs are checkpointed in SQLite (`CHECKPOINT_DB`), so an approval can come from a different process, hours later. The CLI launches the MCP server over stdio. The agent can only call `get_incident`, `update_incident` and `assign_group` (client-side allowlist); `create_incident` exists on the server but isn't needed for triage.

## Roadmap

- [x] **Phase 0: Foundation.** Scaffold, mock ServiceNow API, HITL demo, ServiceNow client (retries, backoff, idempotent create)
- [x] **Phase 1: MCP server.** Tools, resource, error mapping, stdio, contract tests
- [x] **Phase 2: Agent graph + LLM (CLI).** load → classify → retrieve → propose → approve (interrupt) → execute → summarize; SQLite checkpoints; CLI
- [ ] **Phase 3: RAG.** ~15 KB articles, heading-aware chunks with metadata, vector store, citations, `search_kb` tool, retrieval hit-rate check
- [ ] **Phase 4: Guardrails.** Prompt-injection check, tool allowlist, proposal schema validation, approval enforced in code
- [ ] **Phase 5: Evals + CI gate.** 30–50 labelled tickets; category/impact/urgency/priority/group accuracy, tool-call correctness, groundedness; CI pass threshold
- [ ] **Phase 6: Observability + agent API.** JSONL run log (tokens, cost, latency, steps), `/metrics`, approve/reject endpoints
- [ ] **Phase 7: Ship.** Docker, Render deploy, eval results and live link here
- [ ] Phase 8 (optional): React approval page
