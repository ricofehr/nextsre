# DEEP.md — NextSRE Component Reference

---

## Big Picture

NextSRE is a multi-agent incident-response system built on a deliberate privilege split. A **read-only Observer** investigates a reported incident by gathering evidence from Kubernetes, Prometheus alerts, and Elasticsearch, then decides whether a remediation is warranted. If it is, the Observer hands a single, precise instruction to a **write-capable Operator** over the **A2A (Agent-to-Agent) protocol**. The Operator re-evaluates that instruction against its own policy harness and executes it if safe.

The privilege boundary is physical, not just conventional. The Observer process has zero write tools — it cannot mutate infrastructure even if the LLM is manipulated. The Operator has write tools but its MCP servers enforce hard policy (namespace allowlists, replica caps, binary allowlists) that rejects violations before any mutation reaches the cluster, independent of the LLM's judgment. A third optional layer — a LangGraph human-approval checkpointer — can pause execution before writes if `REQUIRE_APPROVAL=1`.

Both agents are stateless HTTP services. All tools come from external MCP servers they connect to at startup, so the tool surface can be audited, versioned, and swapped without touching agent code. A synthetic demo mode (`DEMO_MODE=1`, the default) provides a shared JSON cluster state across all five MCP servers, enabling full end-to-end runs without real infrastructure or a cloud LLM.

```
fire_incident.py
      │  A2A JSON-RPC (port 9300)
      ▼
┌─────────────────────────────────────┐
│  Observer (LangGraph ReAct)         │  read MCP tools only
│   k8s-ro · prom-alerts · es-logs    │
└──────────┬──────────────────────────┘
           │  request_remediation()
           │  A2A JSON-RPC (port 9301)
           ▼
┌─────────────────────────────────────┐
│  Operator (LangGraph ReAct)         │  write MCP tools + harness
│   k8s-write · shell-exec            │
└─────────────────────────────────────┘
```

---

## Read-Only Agent (Observer)

**Entry point:** `agents/observer/server.py` → `agents/observer/observer_agent.py`

The Observer is a LangGraph ReAct agent that loads three read-only MCP servers as its tool surface and one synthetic write tool (`request_remediation`) that actually crosses the network to the Operator.

### Tool surface

At startup `build_observer()` creates a `MultiServerMCPClient` connecting to:

| Logical name | Default URL | Provides |
|---|---|---|
| `k8s_ro` | `http://localhost:8101/mcp` | `list_pods`, `get_pod_logs`, `recent_warning_events` |
| `prometheus_alerts` | `http://localhost:8102/mcp` | `active_alerts`, `query_metric` |
| `es_logs` | `http://localhost:8103/mcp` | `search_logs` |

All three URLs are overridable via `K8S_RO_URL`, `PROMETHEUS_ALERTS_URL`, `ES_LOGS_URL`.

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

`request_remediation` is a LangChain `@tool` that wraps `call_agent` from `agents/a2a_app.py`. It sends the Observer's `action` string as plain text to the Operator's A2A endpoint and returns the Operator's textual report. From the LLM's perspective it looks like any other tool call; underneath it is a full JSON-RPC round-trip to a separate process.

```python
@tool
async def request_remediation(action: str) -> str:
    """Delegate ONE precise infrastructure change to the write agent."""
    return await call_agent(OPERATOR_URL, action)
```

The Observer never sees write MCP tools. Its tool list at runtime is exactly the three MCP servers plus this one bridge.

### Agent card (`agents/observer/card.py`)

Advertises the `investigate` skill with tags `["sre", "read-only", "diagnosis"]`. The card is served at `GET /` on the agent's HTTP port and is the entry point for A2A service discovery.

---

## Write Agent (Operator)

**Entry point:** `agents/operator/server.py` → `agents/operator/operator_agent.py`

The Operator is a LangGraph ReAct agent with two write MCP servers and a three-layer policy harness. It receives a remediation proposal from the Observer but treats it as something to verify, not blindly execute.

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

**Layer 3 — Human approval gate** (optional LangGraph checkpointer):

```python
if env_flag("REQUIRE_APPROVAL", default=False):
    graph = create_react_agent(
        llm, tools,
        checkpointer=InMemorySaver(),
        interrupt_before=["tools"],
    )
```

When enabled, the graph pauses before every tool node. This is the only layer that stops execution _before_ a tool call; layers 1 and 2 operate during or after the LLM's decision.

`LangGraphExecutor` detects the interrupt (`agent.get_state(config).next` is non-empty) and puts the A2A Task into `TASK_STATE_INPUT_REQUIRED` rather than reporting completion — otherwise the caller would receive a plausible-looking report for a remediation that never ran.

**Known limitation:** resuming an approved task is not implemented. `call_agent` opens a fresh context per call, so there is no channel to continue the checkpointed thread. `REQUIRE_APPROVAL=1` therefore halts remediation rather than gating it.

### Agent card (`agents/operator/card.py`)

Advertises the `remediate` skill with tags `["sre", "write", "kubernetes"]`. URL defaults to `http://localhost:9301`.

---

## A2A — Agent-to-Agent Protocol

### What A2A is

A2A (Agent-to-Agent, `a2a-sdk==1.1.2`) is a JSON-RPC-over-HTTP protocol for agents to call other agents as opaque services. An agent publishes an **AgentCard** at `GET /` advertising its skills and transport binding. Callers send tasks via `POST /` using the `message/send` or `message/stream` JSON-RPC method. Responses are streamed as server-sent events carrying `TaskStatusUpdate` and `TaskArtifact` messages until the task reaches a terminal state.

**Important version note:** This project uses A2A 1.x, which is proto-based and structurally different from 0.x. `a2a-sdk==1.1.2` is pinned; floating the version will break imports and message shapes.

### Shared infrastructure (`agents/a2a_app.py`)

#### `build_app(card, executor) -> Starlette`

Creates the ASGI application used by both agents. Mounts the A2A JSON-RPC handler and serves the agent card on `GET /`. Both `observer/server.py` and `operator/server.py` call this with their own card and executor and pass the result to `uvicorn.run`.

#### `LangGraphExecutor`

Bridges a LangGraph `CompiledGraph` to the `AgentExecutor` interface expected by the A2A SDK.

```python
class LangGraphExecutor(AgentExecutor):
    def __init__(self, build_agent: Callable[[], Awaitable]):
        self._build = build_agent
        self._agent = None

    async def _ensure(self):
        if self._agent is None:
            self._agent = await self._build()

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        await self._ensure()
        # streams LangGraph node updates as TaskStatusUpdate events
        # final response becomes a TaskArtifact
```

The executor lazily builds the agent on first call (so MCP server connections are established after the HTTP server is already listening). It iterates `agent.astream_events`, translates each LangGraph node update to a human-readable status string via `_summarize_step`, enqueues those as `TaskStatusUpdate`, and enqueues the last message content as a `TaskArtifact` when the graph terminates.

`_summarize_step` maps LangGraph node names to status strings:
- `"agent"` node → `"Thinking…"`
- `"tools"` node → extracts tool call name, e.g. `"Calling list_pods…"`

#### `call_agent(base_url, text, *, on_status, timeout) -> str`

The A2A client used by `request_remediation` and `fire_incident.py`. Sends a `SendMessageRequest`, iterates the SSE stream, calls `on_status` for each status update, and returns the artifact text from the final response.

```python
async def call_agent(base_url, text, *, on_status=None, timeout=180) -> str:
    req = user_text_request(text)          # wraps in A2A envelope
    async for event in client.send_message_streaming(req):
        kind, text = render_event(event)
        if kind == "status" and on_status:
            on_status(text)
        elif kind == "artifact":
            return text
```

### Message helpers (`common/a2a_helpers.py`)

- `user_text_request(text)` — builds a `SendMessageRequest` with a uuid4 message ID, `ROLE_USER`, and a single `Part(text=text)`.
- `render_event(event)` — demultiplexes the `StreamResponse` union type into `(kind, text)` pairs where `kind` is one of `"status"`, `"artifact"`, `"message"`, `"task"`, `"unknown"`.

---

## LangChain / LangGraph

### LLM factory (`common/llm.py`)

```python
def local_llm(model: str | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        model=os.getenv("LLM_MODEL", "gemma3:27b"),
        api_key=os.getenv("LLM_API_KEY", "local"),
        temperature=0,
        timeout=120,
    )
```

`temperature=0` is intentional: SRE decisions should be deterministic given the same evidence. The `ChatOpenAI` class is used as an OpenAI-compatible client — any server that speaks the `/v1/chat/completions` API works (Ollama, llama.cpp, vLLM, LM Studio, and OpenAI itself).

### ReAct agent construction

Both agents are built with `create_react_agent` from `langgraph.prebuilt`:

```python
llm = local_llm()
async with MultiServerMCPClient(mcp_servers) as mcp:
    tools = mcp.get_tools()
    agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)
    return agent
```

`create_react_agent` compiles a LangGraph `StateGraph` with two nodes: `"agent"` (calls the LLM with the current message list) and `"tools"` (executes the tool calls the LLM returned). The graph loops until the LLM emits a message with no tool calls.

### Tool loading via `MultiServerMCPClient`

`langchain-mcp-adapters==0.3.0` provides `MultiServerMCPClient`, which connects to multiple MCP HTTP endpoints and exposes all their tools as LangChain `BaseTool` objects. Each tool is described by the MCP server's tool schema; the LLM sees the tool name, description, and JSON Schema for its arguments. The client manages the persistent HTTP/SSE connections to each MCP server for the lifetime of the `async with` block.

### The checkpointer and interrupt pattern (Operator only)

When `REQUIRE_APPROVAL=1`:

```python
graph = create_react_agent(
    llm, tools,
    checkpointer=InMemorySaver(),
    interrupt_before=["tools"],
)
```

`interrupt_before=["tools"]` tells LangGraph to pause the graph just before executing tool calls and persist state in the `InMemorySaver`. This is the only mechanism in the system that could give a human veto power over a specific action _after_ the LLM has decided to take it but _before_ it reaches the cluster.

In principle a human inspects the pending tool call and calls `graph.invoke(None, config)` to resume. **That resume path is not wired up here** — see the Known limitation above: `call_agent` opens a fresh context per call, so nothing can continue the checkpointed thread. `REQUIRE_APPROVAL=1` currently halts remediation and reports `TASK_STATE_INPUT_REQUIRED`; it does not gate it.

---

## MCP Implementation

### Architecture pattern

Each MCP server is a standalone Python module that calls `mcp.server.fastmcp.FastMCP` to register tools and runs a Starlette ASGI app on a dedicated port. All five are built from the same Dockerfile and differ only in their tool definitions and environment configuration.

```python
from common.serving import bind_host, port
from . import backends

# Loopback by default; compose sets MCP_BIND_HOST=0.0.0.0. Never hardcode the
# bind address - these servers do not authenticate their callers.
mcp = FastMCP("server-name", host=bind_host(), port=port(default=PORT))

@mcp.tool()
def some_tool(arg: str) -> dict:
    """Policy checks and bounds live here; I/O lives in the backend."""
    return backends.some_domain().some_operation(arg)

if __name__ == "__main__":
    env_flag("DEMO_MODE", default=True)  # fail fast on a malformed value
    mcp.run(transport="streamable-http")
```

### Demo state (`mcp_servers/demo_state.py`)

The synthetic cluster is a single JSON file shared across all five MCP server processes via `DEMO_STATE_FILE`. Reads and writes go through `load()` and `update(mutator)` which acquire a `threading.Lock` and write atomically via `tempfile + os.replace` to avoid partial reads.

The seed state encodes a real-looking incident: `payments-api` in `CrashLoopBackOff` with 47 restarts and `OOMKilled` as the last error, plus a critical alert and a DB connection pool saturation warning. When `restart_deployment` runs in demo mode it calls `update()` to flip `healthy=True` and clear `reason` and `last_error` — the read tools will then report a healthy deployment, confirming recovery.

### Read-only servers

**`k8s_readonly.py` (port 8101)**

In demo mode synthesizes pods from `demo_state` deployments: one pod per replica, all with `phase="Running"` and restarts/reason drawn from the deployment record. In real mode uses the official `kubernetes` client with kubeconfig or in-cluster service account.

`phase` and `reason` are separate fields in both adapters, because they are separate things in Kubernetes: a pod phase is only `Pending|Running|Succeeded|Failed|Unknown`, and a crash-looping pod reports phase `Running`. `CrashLoopBackOff` is a *container waiting reason*. The demo previously reported it as the phase — a contract the real adapter could not satisfy, so the crash-loop signal would have vanished the moment `DEMO_MODE=0` was set against a real cluster.

Tools: `list_pods(namespace)`, `get_pod_logs(name, namespace, tail_lines)`, `recent_warning_events(namespace, limit)`.

**`prometheus_alerts.py` (port 8102)**

In demo mode returns `state["alerts"]` filtered by severity and synthesizes metric results from deployment state. In real mode queries the Prometheus Alertmanager REST API (`ALERTMANAGER_URL`) for `active_alerts` and the Prometheus query API (`PROMETHEUS_URL`) for `query_metric`. Severity ordering is numeric (`info=0, warning=1, critical=2`) for `min_severity` filtering.

Tools: `active_alerts(min_severity)`, `query_metric(query)`.

**`es_logs.py` (port 8103)**

In demo mode synthesizes log entries: healthy deployments emit info messages; unhealthy ones emit OOMKilled errors and crash messages. In real mode issues a `_search` against the `logs-*` index pattern bounded by a time range, using `multi_match` over `["message", "level"]` — deliberately *not* `query_string`, whose operators would let LLM-authored text (derived from attacker-influenceable logs) reach fields this tool must not expose. `""` and `"*"` are special-cased to `match_all`, matching the demo backend. `size` and `minutes` are clamped in `es_logs.py` before the backend is called.

Tool: `search_logs(query, service, minutes, size)`.

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

`restart_deployment` patches the deployment's `spec.template.metadata.annotations` with an `sre-agent/restartedAt` timestamp — the standard rolling-restart idiom. `scale_deployment` updates `spec.replicas` after verifying `0 <= replicas <= MAX_REPLICAS`.

In demo mode both tools call `demo_state.update()` to mutate shared state without touching a real cluster.

**`shell_exec.py` (port 8202)**

This is the weakest point of the whole boundary: an under-constrained binary here walks
around `k8s_write.py` entirely. `_harness_check(command)` works in four passes over the
`shlex.split` argv — never over the raw string, since quoting and whitespace make
raw-string matching trivially evadable:

1. `argv[0]` must be in `ALLOWED_BINARIES` and must not be path-qualified.
2. No token may be in `DENIED_TOKENS` — mutating kubectl subcommands (`delete`, `apply`,
   `exec`, `scale`, …), `secret`/`secrets`, `--raw`, `dump`, and the destructive flags of
   the binaries that take no verb (`journalctl --vacuum-*`, `ss -K`). Position-independent,
   and matched with any `=value` suffix stripped.
3. For binaries in `ALLOWED_VERBS` (`kubectl`, `systemctl`), `argv[1]` must be the verb and
   must not be a flag. Scanning for "the first non-flag token" is unsafe: kubectl global
   flags take values, so `kubectl --cache-dir get delete ns kube-system` would present
   `get` as the verb while kubectl parses `--cache-dir=get` and runs the delete.
4. `timeout_s` is clamped to 1–60 s; it arrives from an LLM.

Any failure raises `PermissionError` with the reason. Passing commands run via
`subprocess.run` without a shell and with a minimal `env` (the server's own environment
holds credentials); stdout/stderr are truncated to the last 4000 / 2000 characters.

`kubectl` is confined to read verbs, so mutations must go through `k8s_write.py` where
they are guarded and audited. `systemctl restart|reload` is deliberately permitted — host
service remediation is what the Operator is for — so this is a *non-destructive* allowlist
rather than a read-only one.

In demo mode the tool returns canned output without executing anything.

---

## How to Integrate ADK (Google Agent Development Kit)

The architecture is intentionally modular: agents are just HTTP services behind an A2A card, tools are MCP servers. ADK can slot in at multiple levels without disrupting the rest.

### Option 1 — Replace LangGraph with ADK agents

ADK's `LlmAgent` is equivalent to LangGraph's `create_react_agent`. It runs a ReAct loop, supports tool calling, and can be wrapped in a streaming HTTP server. The migration path for one agent:

```python
# current (LangGraph)
agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)

# with ADK
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

agent = LlmAgent(
    name="observer",
    model=LiteLlm(model="ollama/gemma3:27b"),
    tools=tools,           # same LangChain tools or rewrite as ADK FunctionTool
    instruction=SYSTEM_PROMPT,
)
```

The `LangGraphExecutor` in `a2a_app.py` would be replaced by ADK's own A2A runner (ADK has native A2A support via `google.adk.runners`), removing the custom streaming bridge.

### Option 2 — Use ADK's built-in A2A client/server

ADK provides `A2AServer` and `A2AClient` that handle the agent card, SSE streaming, and task lifecycle automatically. The current `build_app`, `LangGraphExecutor`, and `call_agent` in `a2a_app.py` could be replaced:

```python
# server side (replaces build_app + LangGraphExecutor)
from google.adk.runners import A2AServer
server = A2AServer(agent=adk_agent, host="0.0.0.0", port=9300)

# client side (replaces call_agent)
from google.adk.a2a import A2AClient
client = A2AClient(base_url=OPERATOR_URL)
result = await client.send_message(action)
```

`request_remediation` in `observer/remediation_tool.py` becomes a thin wrapper around `A2AClient.send_message` — the same one-line change as today but using ADK's client.

### Option 3 — Wrap MCP servers as ADK tools

ADK can load MCP servers directly via `MCPToolset`:

```python
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, SseServerParams

toolset = MCPToolset(
    connection_params=SseServerParams(url="http://localhost:8101/mcp")
)
```

This is the same role that `MultiServerMCPClient` plays today. `MCPToolset` is passed to `LlmAgent` instead of `MultiServerMCPClient.get_tools()`. The five MCP servers (`k8s_readonly`, `prometheus_alerts`, `es_logs`, `k8s_write`, `shell_exec`) require zero changes — they are plain MCP HTTP servers and ADK speaks the same protocol.

### Option 4 — ADK multi-agent orchestration

ADK has a native `SequentialAgent` / `ParallelAgent` / `LoopAgent` hierarchy and a `BaseAgent.create_agent_card()` helper. The Observer→Operator delegation pattern maps directly to ADK's parent-child agent model:

```python
from google.adk.agents import LlmAgent, SequentialAgent

operator = LlmAgent(name="operator", model=..., tools=write_tools)
observer = LlmAgent(
    name="observer",
    model=...,
    tools=read_tools,
    sub_agents=[operator],   # ADK auto-generates the delegation tool
)
```

With `sub_agents`, ADK generates the `transfer_to_operator` tool automatically — the equivalent of the hand-written `request_remediation` tool. The privilege boundary is maintained because `operator` only receives the tools passed to its `LlmAgent` constructor.

### What stays the same under any ADK option

- The five MCP servers are unchanged — they are framework-agnostic.
- The demo state (`demo_state.py`) is unchanged.
- The `docker-compose.yml` service topology (5 MCP + 2 agents) is unchanged.
- The environment variable conventions (`ALLOWED_NAMESPACES`, `MAX_REPLICAS`, `REQUIRE_APPROVAL`, etc.) are unchanged.
- The three-layer harness logic lives in the MCP servers, not the agent framework, so it is preserved regardless of which orchestration layer runs above it.
