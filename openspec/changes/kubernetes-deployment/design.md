# Design

## Context

See proposal.md - Why. Current state that shapes the approach:

- `Dockerfile` copies `common`, `mcp_servers`, `agents`, `scripts` but not `services`, so `python -m services.gateway` fails in compose today. It installs `.` without the `real` extra, so `kubernetes` is absent and every real Kubernetes tool raises. It has no `kubectl`, so `shell-exec` cannot run its main binary.
- The Observer always registers `request_remediation`; `OPERATOR_URL` defaults to `http://localhost:9301`, so an unset URL means "local Operator", not "no Operator".
- `shell-exec`'s binary allowlist is fixed in code (`kubectl`, `systemctl`, `journalctl`, `df`, `free`, `ss`, `dig`, `uptime`). It forwards `KUBERNETES_SERVICE_HOST`/`PORT` and `HOME` to the child, so in-cluster `kubectl` works with the pod's ServiceAccount token.
- `k8s-write` enforces protected namespaces, the `ALLOWED_NAMESPACES` allowlist (empty refuses all), RFC 1123 names, replica bounds and an in-memory circuit breaker.
- With `REQUIRE_APPROVAL=1`, the Operator's tool confirmation leaves its A2A task waiting for input; the Observer's `call_agent` never answers it and the gateway's approval store is never populated. Approval is therefore not usable on the cluster yet.
- The gateway accepts unauthenticated webhooks whenever `WEBHOOK_AUTH_TOKEN` is unset, calls the Observer with `call_agent`'s fixed 180 s timeout, and only logs the report.
- Host validation already exists on every server: loopback, the component's compose service name, and `MCP_ALLOWED_HOSTS`. Nothing authenticates callers; reachability is the authorization model.
- The reference cluster runs Prometheus with its Alertmanager in `kube-monitoring`, Loki behind a basic-auth nginx gateway in `kube-logcollect`, Tempo and the OTel collector in `kube-tracing`, an OpenAI-compatible LLM gateway in front of a local model, Kubernetes 1.35. A GitOps platform layer deploys the workloads; its application project may forbid RBAC objects, so the platform has to allow (and constrain) the RoleBindings NextSRE needs.

## Goals / Non-Goals

**Goals:**
- One image, built by CI, that runs all eight components against real backends.
- Start observe-only; enable autonomous writes later by configuration alone.
- A deployment contract precise enough that a platform chart and the cluster-side changes can be written from `docs/deploy-kubernetes.md` alone.

**Non-Goals:**
- A Helm chart or manifests in this repository. The chart belongs to the platform layer that owns the cluster's namespaces, RBAC and secrets.
- Human approval on the cluster (`REQUIRE_APPROVAL` wiring). Follow-up change.
- New write tools or cluster-level tools (nodes, PVCs, Ceph). The write surface stays `restart_deployment` / `scale_deployment` plus read-only `kubectl`.
- Glob/prefix entries in `ALLOWED_NAMESPACES`: namespaces stay explicit names.
- Multi-arch images: the target cluster is amd64.

## Decisions

### Explicit `OBSERVE_ONLY` flag rather than "unset `OPERATOR_URL` disables remediation"
Unset `OPERATOR_URL` already means `localhost:9301` for the per-terminal run in the README; giving it a second meaning would silently change that path. A strict boolean parsed with the existing `env_flag` fails closed on typos (`OBSERVE_ONLY=ture` raises instead of arming the bridge). It is read in `build_observer()`, following the call-time resolution rule.

With the flag on, the Observer gets an observe-only closing section in its system prompt (recommend, never claim action) and the tool list omits `request_remediation`. The evidence-gathering and untrusted-data parts stay shared so the two modes cannot drift. With the flag off the Observer is exactly today's.

### Enabling writes is two independent keys, not one flag
On the cluster, a write needs (1) `OBSERVE_ONLY=0` on the Observer and (2) the namespace in `k8s-write`'s `ALLOWED_NAMESPACES` **and** a RoleBinding for the `k8s-write` ServiceAccount in that namespace. A platform chart can expose this as one value (writes enabled + namespace list) that sets all of them, but a mistake in any one key still refuses the write. The Operator, `k8s-write` and `shell-exec` are deployed only when writes are enabled: no idle write path in observe-only mode.

### `SHELL_BINARIES` narrows, never widens
A pod has no systemd and `df`/`free` describe the container, so on the cluster those binaries produce misleading evidence. Rather than a "Kubernetes mode", an intersection with the built-in allowlist keeps one harness for hosts and pods, and keeps the allowlist's safety in code: configuration can only remove binaries. Unset keeps today's behavior; a set that intersects to nothing refuses everything (same fail-closed rule as `ALLOWED_NAMESPACES`). Read at call time, like the namespace allowlist.

### `kubectl` from `dl.k8s.io`, pinned and checksum-verified
`ARG KUBECTL_VERSION` (a 1.35 patch release, matching the cluster's minor) with its published SHA-256 per architecture pinned as build args, checked with `sha256sum -c` in the build; a mismatch fails the build. Pinning the checksums (rather than fetching the `.sha256` from the same server at build time) means a compromised download origin cannot serve a matching pair. The architecture comes from BuildKit's `TARGETARCH`, falling back to `dpkg --print-architecture` for the classic builder. Installed to `/usr/local/bin` (on the child `PATH`). Alternative: the distro's package. Rejected: Debian's lags the cluster version, and kubectl supports only ±1 minor of skew.

`kubectl` writes its discovery cache under `$HOME/.kube`; with a read-only root filesystem the guide mounts an `emptyDir` there for `shell-exec`.

### Fail closed at gateway startup, not per request
A startup check in `main()` (a helper the tests call directly) makes a misconfigured deployment crash-loop visibly instead of answering 401 to every alert or 202 to everyone. Demo mode keeps today's behavior so the compose quick start needs no secret.

### Report through the existing notifier
`emit_incident_milestone` already fans out to console, Slack and a generic webhook and records a span event. The gateway calls it after `call_agent` returns: phase `hypothesis` with the report as summary on success (a diagnosis plus recommendation in observe-only mode; the Observer's report includes the action taken when writes are on), phase `failed` on exception or timeout. Service and namespace come from the first firing alert's labels. No new `IncidentPhase`.

Alternative considered: posting the report as a Kubernetes Event or alert annotation. Rejected: needs write RBAC in observe-only mode.

### `OBSERVER_TIMEOUT_S` read at startup
`env_int` in `main()` alongside the token check, default 180 s. The guide recommends 900 s for a local ~15-20 tok/s model, matching the LLM gateway's own request timeout. The Observer's call to the Operator gets the same treatment: `OPERATOR_TIMEOUT_S` (env, default 120 s, previously a constant) so a slow model does not turn enabling writes into a code change. The dedup window becomes `max(300 s, OBSERVER_TIMEOUT_S)` so an alert is not re-dispatched while its own investigation is still running.

### GHCR via GitHub Actions
`docker/login-action`, `docker/metadata-action`, `docker/build-push-action`, SHA-pinned. A `build` job (every event, `contents: read`) builds the image, loads it and smoke-tests it (non-root UID, `import kubernetes, services.gateway`, `kubectl version --client`, gateway `/health`); a `publish` job (pushes only, after `build`) alone holds `packages: write` and reuses the build cache. Only pull-request runs cancel each other, so every push to `main` publishes its SHA. Tags: full SHA always, `main` on the default branch, the version on `v*` tags. PRs build only. Alternative: build in an in-cluster CI. Rejected for now: the code lives on GitHub and the repository is going public; the cluster pulls from GHCR (with an `imagePullSecret` until the package is public).

### Deployment contract lives in `docs/deploy-kubernetes.md`
Generic, with the reference cluster's in-cluster service names as the worked example (no addresses). Kubernetes Service names keep the compose names, so in-namespace callers send `Host: <name>:<port>`, which every server already accepts.

**Components:** observe-only: `k8s-ro`, `prometheus-alerts`, `loki-logs`, `tempo-traces`, `observer`, `gateway`. Writes enabled: plus `operator`, `k8s-write`, `shell-exec`.

**Environment:**

| Variable | Component | Value on the target cluster |
|---|---|---|
| `DEMO_MODE=0`, `MCP_BIND_HOST=0.0.0.0` | all | |
| `OBSERVE_ONLY` | observer | `1` to start; `0` to enable writes |
| `PROMETHEUS_URL` | prometheus-alerts | `http://prometheus-server.kube-monitoring.svc.cluster.local` |
| `ALERTMANAGER_URL` | prometheus-alerts | `http://prometheus-alertmanager.kube-monitoring.svc.cluster.local:9093` |
| `LOKI_URL`, `LOKI_USERNAME`, `LOKI_PASSWORD` | loki-logs | `http://loki-gateway.kube-logcollect.svc.cluster.local`, `loki`, from a copy of `loki-basic-auth` |
| `TEMPO_URL` | tempo-traces | `http://tempo.kube-tracing.svc.cluster.local:3200` |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `_PROTOCOL`, `OTEL_SERVICE_NAME` | all | `http://otel-collector.kube-tracing.svc.cluster.local:4317`, `grpc`, one name per component |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | observer, operator | the LLM gateway's OpenAI-compatible endpoint (`/v1`), the served model id, a dedicated key from a Secret |
| `K8S_RO_URL`, `PROMETHEUS_ALERTS_URL`, `LOKI_LOGS_URL`, `TEMPO_TRACES_URL` | observer | `http://<service>:<port>/mcp` |
| `OPERATOR_URL` | observer (writes) | `http://operator:9301` |
| `K8S_WRITE_URL`, `SHELL_URL` | operator | `http://k8s-write:8201/mcp`, `http://shell-exec:8202/mcp` |
| `REQUIRE_APPROVAL` | operator | `0` (not wired to a human yet) |
| `ALLOWED_NAMESPACES`, `MAX_REPLICAS`, `ALLOW_SCALE_TO_ZERO` | k8s-write | the namespaces with a write RoleBinding; cap; unset |
| `SHELL_BINARIES` | shell-exec | `kubectl` |
| `OBSERVER_URL`, `WEBHOOK_AUTH_TOKEN`, `OBSERVER_TIMEOUT_S` | gateway | `http://observer:9300`, from a Secret shared with Alertmanager, `900` |
| `MCP_ALLOWED_HOSTS` | gateway | add `gateway.<ns>.svc` and `gateway.<ns>.svc.cluster.local` (Alertmanager calls across namespaces) |
| `SLACK_WEBHOOK_URL` / `NOTIFICATION_WEBHOOK_URL` | gateway | optional report sinks |

**RBAC** (one ServiceAccount per component that talks to the API; everything else `automountServiceAccountToken: false`):

| ServiceAccount | Role rules, per bound namespace | Bound in |
|---|---|---|
| `k8s-ro` | `list` on `pods`, `events`; `get` on `pods/log` | watched namespaces |
| `shell-exec` | `get`/`list`/`watch` on `pods`, `pods/log`, `events`, `services`, `endpoints`, `deployments`, `replicasets`, `statefulsets`; `get`/`list` on `pods` in `metrics.k8s.io` (for `kubectl top`) | watched namespaces |
| `k8s-write` | `patch` on `deployments` and `deployments/scale` | write namespaces only |

No rule grants Secrets or ConfigMaps (and the shell harness also denies `secret` tokens). The built-in `view` role is not used: it adds ConfigMaps and every other namespaced type. `shell-exec` still reads workload specs, so inline `env` values are visible to the Operator's model; the guide says to keep credentials in Secrets. Platform namespaces are bound by the NextSRE chart; each application opts in from its own chart with two values: read (binds `k8s-ro` + `shell-exec`) and write (binds `k8s-write`). A ValidatingAdmissionPolicy on the `k8s-write` subject limits its patches to replicas and the `sre-agent/restartedAt` annotation, since `patch` on a Deployment otherwise reaches the whole pod template. A namespace without a binding answers 403, which the tool surfaces as an error.

**NetworkPolicy matrix** (default-deny ingress in the NextSRE namespace):

| To | From | Port |
|---|---|---|
| gateway | Alertmanager pods in `kube-monitoring` | 9302 |
| observer | gateway | 9300 |
| k8s-ro, prometheus-alerts, loki-logs, tempo-traces | observer | 8101-8104 |
| operator | observer | 9301 |
| k8s-write, shell-exec | operator | 8201, 8202 |

The CNI must enforce NetworkPolicy. Egress default-deny is mandatory for `operator` and `shell-exec` once writes are enabled (they act on workload-originated text; the harness is not an exfiltration barrier), recommended elsewhere. Egress needs: kube-apiserver from `k8s-ro`, `shell-exec`, `k8s-write`; Prometheus/Alertmanager, Loki gateway, Tempo, OTel collector, LLM gateway; DNS. On the cluster side, Tempo's NetworkPolicy must admit `tempo-traces` on 3200.

**Enabling writes:** deploy `operator`, `k8s-write`, `shell-exec`; set `ALLOWED_NAMESPACES` and the write RoleBindings for the same namespaces; set `OBSERVE_ONLY=0`. Rollback: `OBSERVE_ONLY=1` (the bridge disappears at the next Observer build) or empty `ALLOWED_NAMESPACES`.

**Cluster-side prerequisites** (separate changes in the platform repositories): Alertmanager's own API (`POST /api/v2/alerts`, unauthenticated) restricted to Prometheus and `prometheus-alerts`, since the webhook token authenticates Alertmanager, not the alert's origin; alerting rules plus an Alertmanager webhook receiver to `http://gateway.<ns>.svc.cluster.local:9302/api/v1/alerts` with `authorization.credentials` = the shared token; Loki credential copy; Tempo policy rule; the NextSRE namespace admitted by the GitOps project; `RoleBinding` permitted in application namespaces and restricted by admission policy to the three NextSRE subjects and their Roles.

## Risks / Trade-offs

- [Autonomous writes driven by a local model] → bounded to restart/scale of deployments in explicitly listed namespaces with RoleBindings, replica floor 1, circuit breaker per resource and per incident; `shell-exec` cannot mutate (verb allowlist + denylist + RBAC). Start in observe-only and compare the recommended remediations with what a human would do before enabling.
- [Circuit breaker and dedup cache are in memory] → `k8s-write` and `gateway` run with one replica; a restart resets the cooldown, which the guide states.
- [Local model too slow for a multi-step investigation] → `OBSERVER_TIMEOUT_S` (900 s documented); the dispatch semaphore (5) bounds concurrency, but the LLM gateway serves 4 parallel requests shared with other clients, so investigations queue under an alert storm.
- [Prompt injection through logs and events] → read-only in observe-only mode; with writes on, the Operator re-evaluates the proposal under its own harness, and the reachable actions are the bounded ones above.
- [Reports may quote log lines carrying sensitive data to Slack] → Slack is opt-in; the guide says so.
- [Workload text rendered by Slack] → summary, service and namespace are mrkdwn-escaped (`&`, `<`, `>`) and the summary is capped at Block Kit's 3000-character section limit.
- [kubectl version skew after a cluster upgrade] → `KUBECTL_VERSION` is a build arg; the guide says to bump it with the cluster minor.
- [GHCR package private while the repository is private] → `imagePullSecret` until the package is public.
- [Real backends have no automated test against live services] → the guide ends with a manual smoke procedure; end-to-end verification happens with the platform change that deploys it.

## Migration Plan

Additive: new behavior is behind `OBSERVE_ONLY`, `SHELL_BINARIES` or `DEMO_MODE=0`. Demo users see the compose gateway start and a reworded gateway prompt. Rollback is reverting the commit; published image tags stay available.

## Open Questions

- Exact LLM gateway Service name and port, and the model id exposed to NextSRE: settled in the platform change; the guide names the variable, not the value.
