# SRE agents: read-only Observer + write Operator, collaborating over A2A

Two SRE agents that split investigation from action across a **privilege
boundary**:

- **Observer** (read-only) investigates an incident using Kubernetes,
  Prometheus alerts, and Elasticsearch logs. It has **no write tools**.
- **Operator** (write) executes remediation on Kubernetes and hosts, behind a
  **policy harness**.

The Observer's only path to changing anything is to delegate to the Operator
**over A2A**. The Operator treats that request as a proposal, re-reasons, and
re-validates every action against its own harness. A2A turns the read/write
split into a real process/network boundary, not a convention.

```
                 A2A (delegation)
  Observer  ───────────────────────►  Operator
 (read-only)                          (write + harness)
   │ MCP                                  │ MCP
   ▼                                      ▼
 K8s(read) · Prometheus alerts · ES logs   K8s(write) · Shell exec
```

Each agent is a **LangGraph** ReAct agent driven by a **local, OpenAI-compatible
LLM** (Ollama or `llama.cpp`'s `llama-server`). Tools come from **MCP servers**
loaded via `langchain-mcp-adapters`. Agent-to-agent communication uses the
**A2A protocol** (`a2a-sdk` 1.x), with the Operator streaming a Task lifecycle
(status updates + a final report artifact).

## What's in the box

| Path | Role |
|---|---|
| `mcp_servers/k8s_readonly.py` | read pods, logs, warning events |
| `mcp_servers/prometheus_alerts.py` | active alerts, PromQL metric query |
| `mcp_servers/es_logs.py` | search recent logs |
| `mcp_servers/k8s_write.py` | restart/scale deployments — **harness: namespace allowlist + replica cap** |
| `mcp_servers/shell_exec.py` | run host commands — **harness: binary allowlist + pattern denylist + verb limits** |
| `mcp_servers/demo_state.py` | shared synthetic "cluster" so the demo runs without real infra |
| `agents/observer/` | read agent: card, MCP read tools, the A2A `request_remediation` delegate tool |
| `agents/operator/` | write agent: card, MCP write tools, optional human approval gate |
| `agents/a2a_app.py` | shared: Starlette app builder, LangGraph→A2A streaming executor, A2A client helper |
| `scripts/fire_incident.py` | reset the seed incident and fire it at the Observer |
| `tests/smoke_test.py` | offline checks (no LLM/cluster): harness, recovery loop, A2A round-trip |

## Demo mode (default)

`DEMO_MODE=1` makes the MCP servers serve a synthetic incident: `payments-api`
is in `CrashLoopBackOff` (OOMKilled). The read servers report it; the write
server's `restart_deployment` flips the shared state to healthy and resolves
the alert — so the Observer's verification re-read actually sees recovery.
No Kubernetes, Postgres, or Elasticsearch required.

## Run it

### 1. Point at your local LLM

You need an OpenAI-compatible endpoint serving a chat model. With Ollama:

```bash
ollama serve
ollama pull gemma3:27b        # or any model your hardware runs
```

Copy `.env.example` to `.env` and set `LLM_MODEL` to whatever your server
actually serves. (`gemma4` isn't a released family; use e.g. a Gemma 3 build.)

### 2. Bring up the stack

```bash
cp .env.example .env
docker compose up --build
```

Seven containers start: five MCP servers plus the two agents. The Observer is
on `localhost:9300`, the Operator on `localhost:9301`.

### 3. Fire the seed incident

From the host (in the repo, with deps installed) or inside a container:

```bash
pip install .                          # or: pip install -e .
python -m scripts.fire_incident
# or a custom prompt:
python -m scripts.fire_incident "why are payments-api pods crashing?"
```

You'll see the Observer's Task stream status updates (`calling tools: ...`)
as it gathers evidence and delegates the fix, then a final incident report.

## Run without Docker

In four terminals (or use `&`), with the venv active and `.env` exported:

```bash
DEMO_STATE_FILE=/tmp/sre/state.json python -m mcp_servers.k8s_readonly   # :8101
DEMO_STATE_FILE=/tmp/sre/state.json python -m mcp_servers.prometheus_alerts  # :8102
DEMO_STATE_FILE=/tmp/sre/state.json python -m mcp_servers.es_logs        # :8103
DEMO_STATE_FILE=/tmp/sre/state.json python -m mcp_servers.k8s_write      # :8201
DEMO_STATE_FILE=/tmp/sre/state.json python -m mcp_servers.shell_exec     # :8202
python -m agents.operator.server                                          # :9301
python -m agents.observer.server                                          # :9300
python -m scripts.fire_incident
```

(Set the same `DEMO_STATE_FILE` for all MCP servers and the runner so they
share one cluster.)

## The harness (defense in depth)

1. **In-tool policy (non-bypassable).** `k8s_write` rejects protected/
   non-allowlisted namespaces and caps replicas; `shell_exec` enforces a binary
   allowlist, a destructive-pattern denylist, and per-binary verb limits. These
   run before any mutation regardless of prompt or caller.
2. **Behavioural policy.** The Operator's system prompt mandates least-
   destructive actions and honest reporting.
3. **Human approval gate (optional).** `REQUIRE_APPROVAL=1` builds the Operator
   graph with `interrupt_before=["tools"]` + a checkpointer, pausing before any
   tool runs so a human can approve/reject.

`tests/smoke_test.py` asserts layer 1 holds (out-of-policy actions raise).

## Switching to real backends

Set `DEMO_MODE=0` and install the extras:

```bash
pip install ".[real]"
```

Then provide credentials/config the real code paths expect: a kubeconfig or
in-cluster service account (`kubernetes`), `ALERTMANAGER_URL` and `PROMETHEUS_URL`
(`prometheus_alerts`), and `ES_URL` (`es_logs`). Keep the Operator's service account
RBAC-scoped to exactly the verbs/namespaces its harness allows — the harness is
your second line, RBAC is your first.

## Notes / gotchas

- `a2a-sdk` 1.x is **proto-based** and differs a lot from 0.x tutorials: no
  `A2AStarletteApplication` (assemble routes via `create_agent_card_routes` +
  `create_jsonrpc_routes`), cards use `supported_interfaces`, text parts are
  `Part(text=...)`, the well-known path is `/.well-known/agent-card.json`, and a
  `Task` must be enqueued before any status event. All handled in `a2a_app.py`.
- Versions are pinned in `pyproject.toml` for exactly this reason.
- The demo state file is last-write-wins across processes — fine for a demo,
  not a datastore.
