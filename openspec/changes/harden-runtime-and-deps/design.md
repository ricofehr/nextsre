# Design

## Context

See `proposal.md` for motivation. NextSRE runs five FastMCP tool servers and two Starlette/A2A LangGraph agent servers (`observer` and `operator`). FastMCP servers already used `TransportSecuritySettings`, while the Starlette A2A apps mounted routes directly without host validation. In `mcp_servers/shell_exec.py`, `_fragments()` split `=value` and resource separators, and `_denied()` checked both `fragment` and `fragment.lower()` against `DENIED_TOKENS`, which meant adding uppercase short flags like `-H`, `-M`, or `-D` to `DENIED_TOKENS` would have blocked lowercase `-h`, `-m`, and `-d`. Meanwhile, only `ss` had an entry in `ALLOWED_FLAGS`; `df`, `free`, `dig`, and `uptime` had no flag allowlist.

## Goals / Non-Goals

**Goals:**
- Prevent command-flag smuggling via attached short-option values (`-f...`, `-k...`, `-H...`, `-M...`) and constrain all verb-less binaries (`ss`, `df`, `free`, `dig`, `uptime`) to explicit safe flags.
- Enforce DNS-rebinding `Host` header validation on both A2A servers using the same `MCP_ALLOWED_HOSTS` configuration as MCP servers.
- Eliminate cold-start race conditions in `LangGraphExecutor._ensure()`, cross-process races in `demo_state.py`, import-time URL freezing in agent modules, and unclosed clients in `mcp_servers/backends/real.py`.
- Replace deprecated `langgraph.prebuilt.create_react_agent` with a minimal local `StateGraph(MessagesState)` builder and upgrade pinned dependencies to current CVE-clean versions on Python 3.12+.
- Run all containers with read-only root filesystems, `/tmp` tmpfs, and `no-new-privileges:true`.

**Non-Goals:**
- Introducing authentication/TLS between internal compose services (documented as out-of-scope for the local demo architecture).
- Migrating `mcp` from `1.x` (`FastMCP`) to `2.x` (`MCPServer`), since `langchain-mcp-adapters==0.3.2` requires `mcp<2.0.0`.

## Decisions

1. **Case-sensitive short-flag matching + attached prefix extraction in `mcp_servers/shell_exec.py`**
   - *Decision*: In `_fragments(token)`, when `base` starts with `-` (and not `--`) and `len(base) > 2`, append `base[:2]` so attached values like `-fhttps://...` or `-Huser@remote` expose `-f` / `-H` to `_denied()`. In `_denied(token)`, check `_is_short_flag(fragment)` with exact case (`fragment in DENIED_TOKENS`), while long flags and subcommands continue to check `fragment in DENIED_TOKENS or fragment.lower() in DENIED_TOKENS`.
   - *Alternatives considered*: Adding a full `getopt`/`argparse` parser per binary. Rejected per KISS: a two-line fragment extraction plus explicit `ALLOWED_FLAGS` for every verb-less binary blocks both attached short flags and unknown options without maintaining per-binary option tables for complex CLIs like `kubectl`.

2. **Shared `allowed_hosts(service)` + Starlette `TrustedHostMiddleware` for A2A servers**
   - *Decision*: Factor `allowed_hosts(service)` out of `transport_security()` in `common/serving.py` and pass the union of `allowed_hosts(service)` and hostnames parsed from `card.supported_interfaces` to `TrustedHostMiddleware` in `agents/a2a_app.py:build_app()`.
   - *Alternatives considered*: Custom ASGI middleware. Rejected because Starlette's built-in `TrustedHostMiddleware` is standard, tested, and already installed.

3. **Custom `build_react_agent()` in `common/llm.py`**
   - *Decision*: Implement `build_react_agent(model, tools, *, prompt, checkpointer=None, interrupt_before=None)` directly over `StateGraph(MessagesState)`, `ToolNode(tools)`, and `tools_condition`.
   - *Alternatives considered*: Switching to `langchain.agents.create_agent`. Rejected because it requires pulling in the full `langchain` distribution rather than keeping the minimal `langchain-core` + `langgraph` footprint.

4. **Cross-process advisory locking in `mcp_servers/demo_state.py`**
   - *Decision*: Introduce `_locked_state_path()` context manager combining the in-process `threading.Lock` with `fcntl.flock(..., fcntl.LOCK_EX)` on a `<state_file>.lock` companion file, plus `try/except BaseException` cleanup around `tempfile.mkstemp` in `_save_unlocked()`.
   - *Alternatives considered*: Locking the state file itself. Rejected because `os.replace(tmp, path)` replaces the inode on every write, which would invalidate locks held on the old inode by concurrent processes.

## Risks / Trade-offs

- [`fcntl.flock` is POSIX-only] → NextSRE targets Linux containers and macOS development environments; documented and covered by tests.
- [`read_only: true` container rootfs could break tools writing to `/tmp`] → Mounted `/tmp` as `tmpfs` via the shared `x-hardening` anchor in `docker-compose.yml` and set `PYTHONDONTWRITEBYTECODE=1` in `Dockerfile`.
