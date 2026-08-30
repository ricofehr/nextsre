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
| `mcp_servers/k8s_write.py` | restart/scale deployments — **harness: namespace allowlist + replica cap + name validation** |
| `mcp_servers/shell_exec.py` | run host commands — **harness: binary allowlist + per-binary non-destructive verb allowlist + token denylist** |
| `mcp_servers/backends/` | demo vs real adapters behind a port per domain; selection is lazy, so the tools stay importable and testable |
| `mcp_servers/demo_state.py` | shared synthetic "cluster" so the demo runs without real infra |
| `common/env.py` | fail-closed env parsing — an unrecognised `DEMO_MODE` raises instead of quietly meaning "not demo" |
| `agents/observer/` | read agent: card, MCP read tools, the A2A `request_remediation` delegate tool |
| `agents/operator/` | write agent: card, MCP write tools, optional human approval gate |
| `agents/a2a_app.py` | shared: Starlette app builder, LangGraph→A2A streaming executor, A2A client helper |
| `scripts/fire_incident.py` | reset the seed incident and fire it at the Observer |
| `tests/` | offline suite (no LLM/cluster): harness regressions, recovery loop, A2A round-trip |

## Demo mode (default)

`DEMO_MODE=1` makes the MCP servers serve a synthetic incident: `payments-api`
is in `CrashLoopBackOff` (OOMKilled). The read servers report it; the write
server's `restart_deployment` flips the shared state to healthy and resolves
the alert — so the Observer's verification re-read actually sees recovery.
No Kubernetes, Prometheus, or Elasticsearch required.

`DEMO_MODE` is parsed strictly: `1/0/true/false/yes/no/on/off` are accepted and
anything else raises — at startup for the MCP servers, and again before any tool
acts. An unrecognised value must not be read as "not demo", which is what a bare
`== "1"` comparison does; that would silently point the write tools at real
infrastructure.

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

Seven containers start: five MCP servers plus the two agents. Only the Observer
is published, on `localhost:9300`; everything else is reachable on the compose
network only (see [Network boundary](#network-boundary)).

### 3. Fire the seed incident

From the host (in the repo, with deps installed) or inside a container:

```bash
pip install .                          # or: pip install -e .
python -m scripts.fire_incident
# or a custom prompt:
python -m scripts.fire_incident "why are payments-api pods crashing?"
```

## Tests and linting

```bash
pip install -e ".[dev]"
pytest                      # full suite, including harness-bypass regressions
python -m tests.smoke_test  # pytest-free demo-flow check (does not assert the harness)
black --check . && isort --check-only . && flake8
pip-audit
```

You'll see the Observer's Task stream status updates (`calling tools: ...`)
as it gathers evidence and delegates the fix, then a final incident report.

## Run without Docker

In seven terminals (or use `&`), with the venv active and `.env` exported. Every
service binds loopback here, so nothing is exposed to the network:

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

1. **In-tool policy.** `k8s_write` rejects protected and non-allowlisted
   namespaces, validates every object name, caps replicas, and refuses scaling to
   zero unless `ALLOW_SCALE_TO_ZERO=1` — a full stop of the service under
   investigation is not something the backstop should certify as in-policy.
   `shell_exec`
   enforces a binary allowlist, a **per-binary verb allowlist pinned to
   `argv[1]`**, a position-independent **token denylist** (mutating kubectl
   subcommands, Secret material, identity/endpoint redirection), and a **flag
   allowlist** for the binaries that take no verb. These run before any mutation
   regardless of prompt or caller.
2. **Behavioural policy.** The Operator's system prompt mandates least-
   destructive actions and honest reporting. The Observer's prompt marks all tool
   output as untrusted data, since logs and alert annotations are written by the
   workloads under investigation.
3. **Human approval gate (optional).** `REQUIRE_APPROVAL=1` builds the Operator
   graph with `interrupt_before=["tools"]` + a checkpointer, pausing before any
   tool runs so a human can approve/reject.

Layer 1 is only as strong as its weakest tool. `shell_exec` must keep `kubectl`
read-only: an unrestricted `kubectl` is a way around *everything* in `k8s_write`
— `kubectl delete`/`apply`/`scale` ignore the namespace allowlist and replica
cap, and `kubectl exec -- sh -c '…'` re-enters a shell and defeats the binary
allowlist itself. `tests/test_harness.py` pins that shut with explicit
regressions; run `pytest` before trusting the boundary.

### Network boundary

None of these services authenticates its caller, so reachability *is* the
authorization model:

- Only the Observer publishes a port (`9300`). The Operator is deliberately not
  published — anything that can reach `9301` drives the write agent directly,
  with the Observer bypassed entirely.
- MCP servers bind `127.0.0.1` unless `MCP_BIND_HOST` says otherwise; compose
  sets `0.0.0.0` because there the container network is the boundary. Anything
  that can open a socket to `shell-exec:8202` gets command execution.
- Host/Origin validation is **on**. `FastMCP` treats an unset `transport_security`
  as "DNS-rebinding protection off" — the inverse of the library's own default —
  so every server passes it explicitly. Without it, a browser on a machine running
  these servers can be steered to the tool surface by an attacker domain that
  re-resolves to `127.0.0.1`: same-origin as far as CORS is concerned, so the
  Host check is the only thing standing in the way. Extend with `MCP_ALLOWED_HOSTS`.
- Containers run as a non-root user.

**Residual risk, accepted for a demo:**

- `dig` stays allowlisted for diagnostics and is a viable DNS exfiltration channel;
  `journalctl` reads host logs (its destructive `--vacuum-*`/`--rotate` flags are denied).
- Secret material is denied, but `kubectl get` still reads any other resource —
  `configmaps`, `serviceaccounts`, pod env — and returns it into the LLM transcript.
  Closing that properly needs a resource *allowlist*, not more denied words; scope
  the Operator's RBAC as the primary control.
- `systemctl restart|reload` is permitted on any unit — deliberate, since host
  service remediation is what the Operator is for, but it is not namespace-scoped
  or audited the way `k8s_write.py` actions are.
- No authentication or TLS anywhere. Do not expose these ports beyond the host.
- Runtime dependencies are pinned to exact versions, but without hashes, and the
  optional `real` extra is version-ranged rather than pinned.
- `REQUIRE_APPROVAL=1` halts remediation rather than gating it — see DEEP.md.

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
- Runtime versions are pinned exactly in `pyproject.toml` for that reason. The
  optional `real` extra is version-ranged, not pinned.
- `mcp` stays on 1.x: `langchain-mcp-adapters` requires `mcp<2.0.0`, and 2.x
  renames `FastMCP` to `MCPServer`. The pin clears PYSEC-2026-3483, which affects
  the deprecated websocket transport this repo does not use.
- The demo state file is last-write-wins across processes — fine for a demo,
  not a datastore.
