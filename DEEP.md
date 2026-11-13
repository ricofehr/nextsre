# DEEP.md — NextSRE Component Reference

---

## Big Picture

NextSRE is a multi-agent incident-response system built on a deliberate privilege split. A **read-only Observer** investigates a reported incident by gathering evidence from Kubernetes, Prometheus alerts, Grafana Loki logs, and Grafana Tempo distributed traces, then decides whether a remediation is warranted. If it is, the Observer hands a single, precise instruction to a **write-capable Operator** over the **A2A (Agent-to-Agent) protocol**. The Operator re-evaluates that instruction against its own policy harness and executes it if safe.

The privilege boundary is physical, not just conventional. The Observer process has zero write tools — it cannot mutate infrastructure even if the LLM is manipulated. The Operator has write tools but its MCP servers enforce hard policy (namespace allowlists, replica caps, binary allowlists) that rejects violations before any mutation reaches the cluster, independent of the LLM's judgment. A third optional layer — an ADK human-approval confirmation gate (`require_confirmation=True`) — can pause execution before writes if `REQUIRE_APPROVAL=1`.

Both agents are stateless HTTP services. All tools come from external MCP servers they connect to at startup, so the tool surface can be audited, versioned, and swapped without touching agent code. A synthetic demo mode (`DEMO_MODE=1`, the default) provides a shared JSON cluster state across all six MCP servers, enabling full end-to-end runs without real infrastructure or a cloud LLM.

```
fire_incident.py
      │  A2A JSON-RPC (port 9300, W3C traceparent)
      ▼
┌───────────────────────────────────────────────────┐
│  Observer (Google ADK LlmAgent)                   │  read MCP tools only
│   k8s-ro · prom-alerts · loki-logs · tempo-traces │
└──────────┬────────────────────────────────────────┘
           │  request_remediation()
           │  A2A JSON-RPC (port 9301, W3C traceparent)
           ▼
┌───────────────────────────────────────────────────┐
│  Operator (Google ADK LlmAgent)                   │  write MCP tools + harness
│   k8s-write · shell-exec                          │
└───────────────────────────────────────────────────┘
```

---

## Read-Only Agent (Observer)

**Entry point:** `agents/observer/server.py` → `agents/observer/observer_agent.py`

The Observer is an ADK `LlmAgent` that loads four read-only MCP servers as its tool surface (`McpToolset`) and one synthetic write tool (`request_remediation`) that actually crosses the network to the Operator.

### Tool surface

At startup `build_observer()` creates four `McpToolset` instances connecting to:

| Logical name | Default URL | Provides |
|---|---|---|
| `k8s_ro` | `http://localhost:8101/mcp` | `list_pods`, `get_pod_logs`, `recent_warning_events` |
| `prometheus_alerts` | `http://localhost:8102/mcp` | `active_alerts`, `query_metric` |
| `loki_logs` | `http://localhost:8103/mcp` | `search_logs` |
| `tempo_traces` | `http://localhost:8104/mcp` | `search_traces`, `get_trace` |

All four URLs are overridable via `K8S_RO_URL`, `PROMETHEUS_ALERTS_URL`, `LOKI_LOGS_URL`, `TEMPO_TRACES_URL`.

### Investigation loop

The system prompt encodes a fixed workflow:

1. Collect evidence: pods + events → Prometheus alerts + metric query → log search.
2. Form a root-cause hypothesis from that evidence.
3. Decide if a change is warranted.
4. If yes, call `request_remediation` with exactly one precise instruction.
5. Re-read signals to confirm resolution.
6. Emit a short incident report (symptom, root cause, action, outcome).

The phrase _"If (and only if)"_ in the prompt is load-bearing: it tells the LLM to default to no-op, not to remediate speculatively.

### The remediation bridge (`agents/observer/remediation_tool.py`)

`request_remediation` is an ADK `FunctionTool` that wraps `call_agent` from `agents/a2a_app.py`. It sends the Observer's `action` string as plain text to the Operator's A2A endpoint and returns the Operator's textual report. From the LLM's perspective it looks like any other tool call; underneath it is a full JSON-RPC round-trip to a separate process.

```python
async def _request_remediation(action: str) -> str:
    """Delegate ONE precise infrastructure change to the write agent."""
    return await call_agent(_operator_url(), action, timeout=OPERATOR_TIMEOUT_S)

request_remediation = FunctionTool(_request_remediation)
```

The Observer never sees write MCP tools. Its tool list at runtime is exactly the four MCP toolsets plus this one bridge.

### Agent card (`agents/observer/card.py`)

Advertises the `investigate` skill with tags `["sre", "read-only", "diagnosis"]`. The card is served at `GET /.well-known/agent-card.json` on the agent's HTTP port and is the entry point for A2A service discovery.

---

## Write Agent (Operator)

**Entry point:** `agents/operator/server.py` → `agents/operator/operator_agent.py`

The Operator is an ADK `LlmAgent` with two write MCP toolsets and a three-layer policy harness. It receives a remediation proposal from the Observer but treats it as something to verify, not blindly execute.

### Tool surface

| Logical name | Default URL | Provides |
|---|---|---|
| `k8s_write` | `http://localhost:8201/mcp` | `restart_deployment`, `scale_deployment` |
| `shell` | `http://localhost:8202/mcp` | `run_command` |

### System prompt design

The prompt sets three behavioural rules on top of the tool-level harness:

- Least destructive first: prefer rolling restart over delete, prefer scale over recreate.
- Never attempt out-of-namespace operations (tools will reject them anyway, but the model should not try).
- Report exactly what ran and what the result was — do not claim success you cannot observe.

### Three-layer harness

**Layer 1 — MCP tool hard policy** (non-bypassable, lives in the MCP servers, described in detail in the MCP section):
- `k8s_write.py` checks `ALLOWED_NAMESPACES` and `MAX_REPLICAS` before touching the cluster.
- `shell_exec.py` validates the binary allowlist, a position-independent token denylist, and a per-binary verb allowlist pinned to `argv[1]`.

**Layer 2 — Behavioural policy** (system prompt): the rules above guide the LLM toward safe choices before it ever calls a tool.

**Layer 3 — Human approval gate** (optional ADK tool confirmation):

```python
require_confirmation = env_flag("REQUIRE_APPROVAL", default=False)
toolsets = list(_mcp_toolsets(require_confirmation=require_confirmation).values())
return build_adk_agent(
    local_llm(),
    toolsets,
    prompt=SYSTEM,
    name="operator",
    require_confirmation=require_confirmation,
)
```

When enabled, ADK's confirmation gate pauses before any write tool runs and emits an `adk_request_confirmation` long-running tool call. `A2aAgentExecutor` converts this into `TASK_STATE_INPUT_REQUIRED` rather than reporting completion — otherwise the caller would receive a plausible-looking report for a remediation that never ran.

**Known limitation:** resuming an approved task is not implemented. `call_agent` opens a fresh context per call, so there is no channel to continue the paused session. `REQUIRE_APPROVAL=1` therefore halts remediation rather than gating it.

### Agent card (`agents/operator/card.py`)

Advertises the `remediate` skill with tags `["sre", "write", "kubernetes"]`. URL defaults to `http://localhost:9301`.

---

## A2A — Agent-to-Agent Protocol

### What A2A is

A2A (Agent-to-Agent, `a2a-sdk==1.3.0`) is a JSON-RPC-over-HTTP protocol for agents to call other agents as opaque services. An agent publishes an **AgentCard** at `GET /.well-known/agent-card.json` advertising its skills and transport binding. Callers send tasks via `POST /` using the `message/send` or `message/stream` JSON-RPC method. Responses are streamed as server-sent events carrying `TaskStatusUpdate` and `TaskArtifact` messages until the task reaches a terminal state.

**Important version note:** This project uses A2A 1.x, which is proto-based and structurally different from 0.x. `a2a-sdk==1.3.0` is pinned; floating the version will break imports and message shapes.

### Shared infrastructure (`agents/a2a_app.py`)

#### `build_app(card, executor, *, service=None) -> Starlette`

Creates the ASGI application used by both agents. Mounts the A2A JSON-RPC handler at `POST /`, serves the agent card on `GET /.well-known/agent-card.json`, and wraps the app in `TrustedHostMiddleware` (DNS-rebinding protection). Both `observer/server.py` and `operator/server.py` call this with their own card and executor and pass the result to `uvicorn.run`.

#### `ADKExecutor`

Bridges an ADK `LlmAgent` builder to the `AgentExecutor` interface expected by the A2A SDK, delegating execution to ADK's built-in `A2aAgentExecutor` and `InMemoryRunner`.

```python
class ADKExecutor(AgentExecutor):
    def __init__(self, build_agent: Callable[[], Awaitable[Any] | Any]):
        self._build = build_agent
        self._agent = None
        self._runner = None
        self._lock = asyncio.Lock()
        self._delegate = A2aAgentExecutor(runner=self._ensure_runner)
```

The executor lazily builds the agent and `InMemoryRunner` on first call under an `asyncio.Lock` (so concurrent cold-start requests build the agent at most once), delegates event conversion and task lifecycle management to `A2aAgentExecutor`, and sanitizes unexpected failure messages so internal URLs or stack traces are not leaked to remote callers.

#### `call_agent(base_url, text, *, on_status, timeout) -> str`

The A2A client used by `request_remediation` and `fire_incident.py`. Sends a `SendMessageRequest`, iterates the SSE stream, calls `on_status` for each status update, and returns the artifact text from the final response.

```python
async def call_agent(base_url, text, *, on_status=None, timeout=180) -> str:
    req = user_text_request(text)          # wraps in A2A envelope
    async for event in client.send_message(req):
        kind, value = render_event(event)
        if kind == "status" and on_status:
            on_status(value)
        elif kind == "artifact":
            artifact_text = value
```

### Message helpers (`common/a2a_helpers.py`)

- `user_text_request(text)` — builds a `SendMessageRequest` with a uuid4 message ID, `ROLE_USER`, and a single `Part(text=text)`.
- `render_event(event)` — demultiplexes the `StreamResponse` union type into `(kind, text)` pairs where `kind` is one of `"status"`, `"artifact"`, `"message"`, `"task"`, `"unknown"`.

---

## Google ADK Runtime

### LLM factory (`common/llm.py`)

```python
def local_llm(model: str | None = None) -> OpenAILlm:
    return OpenAILlm(
        model=model or os.environ.get("LLM_MODEL", "gemma3:27b"),
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("LLM_API_KEY", "local"),
    )
```

`temperature=0` is configured on the `LlmAgent`'s `GenerateContentConfig`: SRE decisions should be deterministic given the same evidence. `OpenAILlm` (`google.adk.integrations.openai`) is used as an OpenAI-compatible client — any server that speaks the `/v1/chat/completions` API works (Ollama, llama.cpp, vLLM, LM Studio, and OpenAI itself).

### Agent construction

Both agents are built with `build_adk_agent` (aliased as `build_react_agent`) from `common/llm.py`, which constructs an ADK `LlmAgent`:

```python
read_toolsets = list(_mcp_toolsets().values())
agent = build_adk_agent(local_llm(), [*read_toolsets, request_remediation], prompt=SYSTEM, name="observer")
```

### Tool loading via `McpToolset`

`google.adk.tools.mcp_tool.McpToolset` with `StreamableHTTPConnectionParams` connects to each MCP HTTP endpoint (`streamable_http` transport) and exposes all its tools to the ADK `LlmAgent`. Each tool is described by the MCP server's tool schema; the LLM sees the tool name, description, and JSON Schema for its arguments.

### Tool confirmation gate (Operator only)

When `REQUIRE_APPROVAL=1`:

```python
require_confirmation = env_flag("REQUIRE_APPROVAL", default=False)
toolsets = list(_mcp_toolsets(require_confirmation=require_confirmation).values())
agent = build_adk_agent(
    local_llm(),
    toolsets,
    prompt=SYSTEM,
    name="operator",
    require_confirmation=require_confirmation,
)
```

`require_confirmation=True` tells ADK's tool execution pipeline (`apply_confirmation_gate`) to hold back unconfirmed tool calls and emit an `adk_request_confirmation` function call instead of executing the tool. `A2aAgentExecutor` surfaces this as `TASK_STATE_INPUT_REQUIRED`.

---

## MCP Implementation

### Architecture pattern

Each MCP server is a standalone Python module that calls `mcp.server.mcpserver.MCPServer` to register tools and runs a Starlette ASGI app on a dedicated port. All six are built from the same Dockerfile and differ only in their tool definitions and environment configuration.

```python
from common.serving import bind_host, port, transport_security
from . import backends

# Loopback by default; compose sets MCP_BIND_HOST=0.0.0.0. Never hardcode the
# bind address - these servers do not authenticate their callers.
mcp = MCPServer("server-name")

@mcp.tool()
def some_tool(arg: str) -> dict:
    """Policy checks and bounds live here; I/O lives in the backend."""
    return backends.some_domain().some_operation(arg)

if __name__ == "__main__":
    env_flag("DEMO_MODE", default=True)  # fail fast on a malformed value
    mcp.run(
        transport="streamable-http",
        host=bind_host(),
        port=port(default=PORT),
        transport_security=transport_security("server-name"),
    )
```

### Demo state (`mcp_servers/demo_state.py`)

The synthetic cluster is a single JSON file shared across all six MCP server processes via `DEMO_STATE_FILE`. Reads and writes go through `load()` and `update(mutator)` which acquire both an in-process `threading.Lock` and an advisory cross-process `fcntl.flock` lock file, writing atomically via `tempfile + os.replace` to avoid partial reads or lost updates.

The seed state encodes a real-looking incident: `payments-api` in `CrashLoopBackOff` with 47 restarts and `OOMKilled` as the last error, plus a critical alert and a DB connection pool saturation warning. When `restart_deployment` runs in demo mode it calls `update()` to flip `healthy=True` and clear `reason` and `last_error` — the read tools will then report a healthy deployment, confirming recovery.

### Read-only servers

**`k8s_readonly.py` (port 8101)**

In demo mode synthesizes pods from `demo_state` deployments: one pod per replica, all with `phase="Running"` and restarts/reason drawn from the deployment record. In real mode uses the official `kubernetes` client with kubeconfig or in-cluster service account.

`phase` and `reason` are separate fields in both adapters, because they are separate things in Kubernetes: a pod phase is only `Pending|Running|Succeeded|Failed|Unknown`, and a crash-looping pod reports phase `Running`. `CrashLoopBackOff` is a *container waiting reason*. The demo previously reported it as the phase — a contract the real adapter could not satisfy, so the crash-loop signal would have vanished the moment `DEMO_MODE=0` was set against a real cluster.

Tools: `list_pods(namespace)`, `get_pod_logs(name, namespace, tail_lines)`, `recent_warning_events(namespace, limit)`.

**`prometheus_alerts.py` (port 8102)**

In demo mode returns `state["alerts"]` filtered by severity and synthesizes metric results from deployment state. In real mode queries the Prometheus Alertmanager REST API (`ALERTMANAGER_URL`) for `active_alerts` and the Prometheus query API (`PROMETHEUS_URL`) for `query_metric`. Severity ordering is numeric (`info=0, warning=1, critical=2`) for `min_severity` filtering.

Tools: `active_alerts(min_severity)`, `query_metric(query)`.

**`loki_logs.py` (port 8103)**

In demo mode synthesizes log entries with correlated 32-hex `trace_id` values: healthy deployments emit info messages; unhealthy ones emit OOMKilled errors and crash messages. In real mode queries Grafana Loki's `/loki/api/v1/query_range` HTTP API (`LOKI_URL`, plus optional `LOKI_USERNAME` / `LOKI_PASSWORD` basic auth) using `build_logql(query, service)`, which escapes double quotes and regex characters so LLM-authored arguments cannot inject LogQL pipeline stages. Structured JSON log lines (Fluent Bit / OTLP) and regex `trace_id` tokens are parsed into `{@timestamp, service, level, message, trace_id}`. `size` and `minutes` are clamped in `loki_logs.py` before the backend is called.

Tool: `search_logs(query, service, minutes, size)`.

**`tempo_traces.py` (port 8104)**

In demo mode synthesizes distributed traces matching the deterministic `trace_id` values emitted by `DemoLogs` and `DemoK8sRead.pod_logs`: unhealthy deployments return `status="error"` traces and error spans; healthy deployments return `status="ok"` traces. In real mode queries Grafana Tempo's HTTP API (`TEMPO_URL`): `GET /api/search` using `build_traceql(service, min_duration_ms, status)` with escaped string literals and validated status values, and `GET /api/traces/<trace_id>` after verifying `trace_id` is a 16- or 32-character hexadecimal string (preventing path traversal).

Tools: `search_traces(service, min_duration_ms, status, minutes, limit)`, `get_trace(trace_id)`.

### Write servers

**`k8s_write.py` (port 8201)**

The harness lives in `_guard_target(name, namespace)`, which validates both the object
name and the namespace as RFC 1123 labels and then applies `_guard_ns`:

```python
PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "default"}

def _guard_ns(namespace: str) -> None:
    _guard_name(namespace, "namespace")
    if namespace in PROTECTED_NAMESPACES:
        raise PermissionError(f"namespace '{namespace}' is protected; writes refused")
    allowed = env_csv("ALLOWED_NAMESPACES", default=DEFAULT_ALLOWED_NAMESPACES)
    if not allowed:                       # parses empty -> refuse everything
        raise PermissionError("ALLOWED_NAMESPACES is empty; all writes refused")
    if namespace not in allowed:
        raise PermissionError(f"namespace '{namespace}' not in allowlist {sorted(allowed)}")
```

`env_csv` strips whitespace and drops blank entries, so `"web, payments"` works and a
trailing comma cannot admit the empty namespace. The allowlist is read per call, not
frozen at import.

`restart_deployment` patches the deployment's `spec.template.metadata.annotations` with an `sre-agent/restartedAt` timestamp — the standard rolling-restart idiom. `scale_deployment` updates `spec.replicas` after verifying `1 <= replicas <= MAX_REPLICAS` (or `0 <= replicas <= MAX_REPLICAS` when `ALLOW_SCALE_TO_ZERO=1` is explicitly set).

In demo mode both tools call `demo_state.update()` to mutate shared state without touching a real cluster.

**`shell_exec.py` (port 8202)**

This is the weakest point of the whole boundary: an under-constrained binary here walks
around `k8s_write.py` entirely. `_harness_check(command)` works in five passes over the
`shlex.split` argv — never over the raw string, since quoting and whitespace make
raw-string matching trivially evadable:

1. `argv[0]` must be in `ALLOWED_BINARIES` and must not be path-qualified.
2. No token may match `DENIED_TOKENS` — mutating kubectl subcommands (`delete`, `apply`,
   `exec`, `scale`, …), `secret`/`secrets`, `--raw`, `dump`, `-f`/`-k` (including attached
   values like `-fhttps://...`), remote/root systemctl flags (`-H`, `-M`, `--root`,
   including `-Huser@remote`), and destructive/path journalctl flags (`--vacuum-*`,
   `--rotate`, `-D`, `--directory`, `--file`). Position-independent, with `=value`
   stripped, leading short flags extracted, and resource separators (`/`, `,`, `.`) split.
3. For binaries in `ALLOWED_VERBS` (`kubectl`, `systemctl`), `argv[1]` must be the verb and
   must not be a flag. Scanning for "the first non-flag token" is unsafe: kubectl global
   flags take values, so `kubectl --cache-dir get delete ns kube-system` would present
   `get` as the verb while kubectl parses `--cache-dir=get` and runs the delete.
4. For verb-less binaries in `ALLOWED_FLAGS` (`ss`, `df`, `free`, `dig`, `uptime`), every
   flag (with clustered short flags expanded) must appear on that binary's allowlist.
5. `timeout_s` is clamped to 1–60 s; it arrives from an LLM.

Any failure raises `PermissionError` with the reason. Passing commands run via
`subprocess.run` without a shell and with a minimal `env` (the server's own environment
holds credentials); stdout/stderr are truncated to the last 4000 / 2000 characters.

`kubectl` is confined to read verbs, so mutations must go through `k8s_write.py` where
they are guarded and audited. `systemctl restart|reload` is deliberately permitted — host
service remediation is what the Operator is for — so this is a *non-destructive* allowlist
rather than a read-only one.

In demo mode the tool returns canned output without executing anything.

---

## Google ADK Integration Summary

NextSRE uses `google-adk[a2a,mcp,openai]` across all three agent layers:

1. **Agent orchestration (`common/llm.py`)**: Both Observer and Operator are ADK `LlmAgent` instances backed by `OpenAILlm` (`google.adk.integrations.openai`) pointed at the local OpenAI-compatible inference server (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`).
2. **A2A server execution (`agents/a2a_app.py`)**: `ADKExecutor` wraps ADK's `A2aAgentExecutor` and `InMemoryRunner` with single-flight lazy initialization (`asyncio.Lock`) and sanitized error reporting, served behind Starlette's `TrustedHostMiddleware`.
3. **MCP toolsets & confirmation gate (`agents/observer/`, `agents/operator/`)**: Tools are loaded via `McpToolset(connection_params=StreamableHTTPConnectionParams(url=...))`. When `REQUIRE_APPROVAL=1`, the Operator passes `require_confirmation=True` to its MCP toolsets so ADK pauses before executing any write tool.
4. **Framework-independent MCP servers (`mcp_servers/`)**: The six MCP servers, demo state (`demo_state.py`), `docker-compose.yml` topology, OpenTelemetry instrumentation (`common/telemetry.py`), and three-layer harness remain completely decoupled from the agent framework.
