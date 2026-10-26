# submissions-agent

MCP server + CLI tools to manage short story submissions, match works to open
calls, build draft timelines, and track Instagram activity.

## Quick start

```bash
# 1. First time only
./scripts/setup.sh

# 2. Every session (cold → hot)
./scripts/start.sh            # starts Ollama + web server
./scripts/start.sh --open     # also opens http://localhost:8765 in browser
./scripts/start.sh --jaeger   # also starts Jaeger tracing (requires Docker)
```

## Install

```bash
pip install -e .
playwright install chromium
cp .env.example .env   # fill in ANTHROPIC_API_KEY, BW_CLIENTID, BW_CLIENTSECRET, TWEEK_API_KEY
```

## Commands

| Command | Description |
|---|---|
| `submissions-agent` | Start the MCP server (stdio transport) |
| `build-profile` | Build or update the works profile from `src/profile/works.json` |
| `run-agent` | Run the multi-agent CLI (flags: `--backend claude\|ollama`, `--verbose`) |
| `web-server` | Start the FastAPI web UI at `http://localhost:8765` |
| `run-evals` | Run the agent eval suite (see Eval suite section) |
| `rag-index` | Rebuild the RAG vector store from the event log (see RAG section) |

## Web UI

```bash
web-server
# open http://localhost:8765
```

Tabs: **Boutique** · **Matches** · **Queries** · **Publishers** · **Works** · **Submissions** · **Saved** · **Schedule**

## Agent

```bash
run-agent "find matches for my completed short fiction"
run-agent --backend claude "build a timeline for River Mouth starting today"
run-agent --verbose "what boutique calls are open this month?"
```

The agent never submits a story or calls a write tool without explicit
confirmation at the terminal prompt.

## Eval suite

```bash
run-evals                           # run all cases (exit 0 = all pass, 1 = any fail)
run-evals --list                    # list all available case ids
run-evals --suite matching          # run all cases whose id starts with "matching/"
run-evals --suite security          # run security / prompt-injection cases only
run-evals --case matching/basic     # run one case by exact id
run-evals --verbose                 # print full agent output and tool call log
```

### Adding a case

Add an `EvalCase` to `src/evals/cases/matching.py` or `security.py` (or a new
file imported in `src/evals/cases/__init__.py`). Built-in assertion helpers:

```python
contains("Ploughshares")        # output must mention this text
not_contains("BW_CLIENTID")     # output must not mention this text
called_tool("match_works")      # agent must have called this tool
not_called_tool("upsert_work")  # agent must not have called this tool
no_write_tools()                # shorthand: no record/upsert/timeline calls
output_not_empty()              # agent must produce a response
max_tool_calls(6)               # runaway loop guard
```

### Fixture files

`evals/fixtures/` holds fixed JSON snapshots used as fake tool responses.
Add or edit them to cover new scenarios without touching live data.

## Project structure

```
src/
  server.py          MCP server entry point
  agent/
    runner.py        run-agent CLI
    loop.py          shared agent loop (security gates live here)
    backends/        claude_backend.py, ollama_backend.py
    agents/          orchestrator, listing, matching, schedule
  evals/
    cli.py           run-evals entry point
    case.py          EvalCase dataclasses + assertion helpers
    runner.py        fixture injector + case runner
    report.py        terminal output + exit code
    cases/           matching.py, security.py
  profile/
    builder.py       build-profile CLI
  web/
    app.py           FastAPI server
    static/          index.html (single-file frontend)
  match/             matcher.py
  timeline/          builder.py
  instagram/         log.py
  sites/             chillsubs.py
  secrets/           bitwarden.py
evals/
  fixtures/          works.json, chillsubs_mini.json
data/                (gitignored) cache, timelines, saved calls, instagram log
```

## Observability (OpenTelemetry)

Tracing is **opt-in** — off by default, zero overhead when disabled.

### Quick start with Jaeger

```bash
# 1. Start Jaeger (all-in-one, Docker)
docker run -d --name jaeger \
  -p 16686:16686 \
  -p 4317:4317 \
  jaegertracing/all-in-one:latest

# 2. Run the app with tracing on
OTEL_ENABLED=true \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
web-server

# 3. Open the trace viewer
open http://localhost:16686
```

### Console mode (no Docker)

```bash
OTEL_ENABLED=true web-server        # prints spans to stdout
OTEL_ENABLED=true run-agent "..."   # CLI runs are also traced
```

### What gets traced

| Span | Attributes |
|---|---|
| `agent.loop` | label, backend, model, iterations, total tool calls, response chars |
| `agent.tool` | name, is_write, approved, result chars, truncated |
| `event.append` | event type, event id |
| `rag.embed` | model, text chars, vector dims |
| `rag.search` | k, result count, store size |
| HTTP calls (auto) | all Ollama, Anthropic, and embedding requests via httpx |
| FastAPI routes (auto) | method, path, status code, latency |

A single `run-agent` call produces a trace like:

```
agent.loop [matching, claude-opus-5-5]         450ms
  ├── POST api.anthropic.com/v1/messages         310ms   ← LLM call 1 (selects tools)
  ├── agent.tool [list_works]                      2ms
  ├── agent.tool [match_works]                     4ms
  ├── agent.tool [get_submission_context]         85ms
  │     ├── rag.embed                             70ms
  │     │     └── POST localhost:11434/api/embed  68ms
  │     └── rag.search                             1ms
  └── POST api.anthropic.com/v1/messages         240ms   ← LLM call 2 (final response)
```

### Environment variables

```
OTEL_ENABLED=true                               # activate (default: off)
OTEL_SERVICE_NAME=submissions-agent             # label in Jaeger UI
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317  # OTLP/gRPC; console if unset
```

## Save for Later

Any card in the UI — submission calls, agents, publishers, small presses, MSWL
wishlists, novel matches — has a **☆ Save** button. Clicking it saves the entry
without submitting, toggling to **★ Saved**. Clicking again removes it.

Saved items live in `data/saved-calls.json` (gitignored) and appear in the
**Saved** tab, grouped by type:

| Group | Source tabs |
|---|---|
| Submission Calls | Boutique, Matches |
| Literary Agents | Agents (QueryTracker) |
| Publishers | Agents (QueryTracker) |
| Small Presses | Publishers (P&W) |
| MSWL Wishlists | Queries → Wishlists |

Each saved item shows metadata chips (deadline, word limit, agency, reading
period) and an inline notes textarea — edits are saved on blur via
`PATCH /api/saved/{id}`.

REST API: `GET/POST /api/saved`, `PATCH /api/saved/{id}`, `DELETE /api/saved/{id}`

## Boutique deadline filter

The Boutique tab toolbar has two filters:

- **Title filter** — live text search across publication name and call title
- **Deadline filter** — month picker populated from the loaded data:
  - *Any deadline* — show all
  - *Open-ended* — calls with no deadline set
  - *Nov 2026*, *Dec 2026*, … — calls whose deadline falls in that month

Both filters compose — you can show "no-fee December calls mentioning 'grief'"
by combining title search with the month filter.

## Event-driven submission tracking

Every submission and response is appended to `data/events.jsonl` — an immutable,
append-only log. The current state of any submission is derived by replaying it.

Events:
- `submission_sent` — emitted by `POST /api/record` and the `record_submission` MCP tool
- `response_received` — emitted by `PATCH /api/submissions/{id}/{idx}` and the new `record_response` MCP tool
- `withdrawal_sent` — emitted when result is set to "withdrawn"

Handlers fire synchronously on every append and are registered in `src/events/handlers.py`.
Adding a new reaction (Slack ping, Instagram draft queue, Tweek follow-up) is one function + one `subscribe()` call.

## RAG over submission history

The matching agent can retrieve past submission outcomes semantically similar to a
given work + call pairing. This surfaces patterns — venues that respond to Trevor's
voice, calls whose description matches his themes — from real track record.

**First-time setup:**
```bash
ollama pull nomic-embed-text   # ~274MB, one-time download
rag-index                      # index all past submissions (after some history exists)
```

**Checking the store:**
```bash
rag-index --stats
```

**How it works:** when the matching agent recommends a venue, it calls
`get_submission_context` which embeds the work synopsis + call description, searches
`data/chroma/` for similar past submissions, and returns formatted outcomes like:

```
Past submission outcomes (most similar first):
  ✗ "Grief in Winter" → Ploughshares: rejected  (2026-03-12)
  ✓ "The Cartographer" → Cartography & Letters: accepted  (2026-01-08)
```

The RAG store auto-updates: every `response_received` event triggers re-indexing via
the handler in `src/events/handlers.py`.

## Environment variables (`.env`)

```
ANTHROPIC_API_KEY=...
BW_CLIENTID=...
BW_CLIENTSECRET=...
TWEEK_API_KEY=...
CLAUDE_MODEL=claude-opus-5-5    # optional override
OLLAMA_HOST=http://localhost:11434  # optional override
EMBED_MODEL=nomic-embed-text        # optional override
```

Bitwarden master password is prompted at runtime via `getpass` — never stored.
