# Proposal

## Why

NextSRE has only ever run in demo mode under docker-compose. Running it on a real cluster (Prometheus/Alertmanager, Loki, Tempo and an OpenTelemetry collector, behind a GitOps platform layer) fails before the first investigation: the image cannot start the gateway and lacks the Kubernetes client, real mode accepts unauthenticated webhooks, and there is no way to run the Observer without its write path. The first milestone on a real cluster is **observe-only**: alerts in, evidence gathered from Prometheus, Loki and Tempo, a diagnosis out, and nothing able to change the cluster. The write path (Operator, `k8s-write`, `shell-exec`) must be ready alongside it, so that enabling autonomous remediation is a configuration change (`OBSERVE_ONLY=0` plus the target namespaces), not another development cycle.

## What Changes

- **Image fixes**: the image ships `services/` (the gateway), the `real` extras (`kubernetes` client) and a pinned, checksum-verified `kubectl`, so one image runs every component, `shell-exec` included, in `DEMO_MODE=0`.
- **Shell harness narrowing** (`SHELL_BINARIES`, comma-separated): restricts `shell-exec` to a subset of its built-in allowlist; it can never add a binary. On a cluster it is set to `kubectl`, since host tools (`df`, `free`, `systemctl`, ...) would only describe the pod they run in.
- **Image delivery**: a GitHub Actions workflow builds the image and publishes it to GHCR on pushes to `main` and on version tags, tagged with the commit SHA.
- **Observe-only mode** (`OBSERVE_ONLY=1`, strict boolean): the Observer is built without `request_remediation` and its prompt asks for a recommended remediation in the report instead of a delegated one. Turning it off restores the A2A bridge to the Operator; writes are then autonomous, bounded by RBAC and the existing harness (namespace allowlist, protected namespaces, replica bounds, restart/scale-only tools, circuit breaker). `REQUIRE_APPROVAL` stays off on the cluster: its confirmation is not wired to a human yet.
- **Gateway, real mode fails closed**: with `DEMO_MODE=0` the gateway refuses to start without `WEBHOOK_AUTH_TOKEN`.
- **Gateway publishes the report**: the Observer's final report (or the dispatch failure) is sent through the configured notifiers (console, Slack, generic webhook), not only written to the gateway log.
- **Gateway dispatch timeout** is configurable (`OBSERVER_TIMEOUT_S`, default unchanged at 180 s): a local model at ~15-20 tok/s needs several minutes per investigation.
- **Gateway prompt** no longer tells the Observer to "remediate if safe"; the Observer's own prompt decides, so the instruction cannot contradict observe-only mode.
- **Docs**: a Kubernetes deployment guide with the cluster contract for both modes: backend env vars (Prometheus, Alertmanager, Loki and its basic auth, Tempo, OTel collector, OpenAI-compatible LLM gateway), one ServiceAccount and its RBAC per component (read for `k8s-ro` and `shell-exec`, patch on deployments for `k8s-write`), `MCP_ALLOWED_HOSTS`, the NetworkPolicy matrix the unauthenticated services rely on, and the procedure to enable writes.

Out of scope (separate changes in the platform repositories): the cluster side (NextSRE namespace, Alertmanager API lockdown, rules and webhook receiver, Tempo NetworkPolicy rule, Loki credential copy, `RoleBinding` allowed in application namespaces behind an admission policy) and the deployment itself (a NextSRE chart, its GitOps application, an LLM gateway key, the opt-in `RoleBinding` in application charts). Wiring `REQUIRE_APPROVAL` to a human (Slack/webhook card resuming the paused Operator tool) is a follow-up change.

## Capabilities

### New Capabilities
- `observe-only-mode`: running the Observer with no path to any mutation, and what it reports instead.
- `container-image`: the single runtime image's contents and how it is built and published.

### Modified Capabilities
- `shell-and-http-hardening`: new operator-configurable narrowing of the shell binary allowlist.
- `incident-ingestion-gateway`: fail-closed webhook authentication in real mode, report publication through notifiers, configurable Observer dispatch timeout, remediation-neutral investigation prompt.

## Impact

- Code: `Dockerfile`, `agents/observer/observer_agent.py`, `mcp_servers/shell_exec.py`, `services/gateway.py`, `common/env.py` (if a helper is needed), `.env.example`, `docker-compose.yml` (gateway now actually starts).
- New: `.github/workflows/image.yml`, `docs/deploy-kubernetes.md`.
- Docs: `README.md` (real backends section links the guide; observe-only), `DEEP.md` (gateway and observer sections).
- Tests: `tests/test_gateway.py`, `tests/test_privilege_boundary.py` (observe-only tool surface), `tests/test_harness.py` (binary narrowing).
- No API change for existing demo users: every new behavior is behind `OBSERVE_ONLY` or `DEMO_MODE=0`, and defaults are unchanged.
