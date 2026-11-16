# Tasks

## 1. Container image

- [x] 1.1 `Dockerfile`: copy `services/` and install `.[real]` in both install steps; keep the non-root `sre` user. Verify: the image builds (Apple `container build`, or the CI job from 2.1 on the PR), and a container from it answers `GET /health` on the gateway, imports `kubernetes`, and `id -u` prints `10001`.
- [x] 1.2 `docker-compose.yml`: confirm the `gateway` service starts with the fixed image (no compose change expected beyond comments). Verify: covered by 1.1's `/health` check, run with the compose command line.
- [x] 1.3 `Dockerfile`: install `kubectl` from `dl.k8s.io` with `ARG KUBECTL_VERSION` (latest 1.35 patch) and `ARG TARGETARCH`, verified with `sha256sum -c` against the published `.sha256`, into `/usr/local/bin`. Verify: `kubectl version --client` in the container prints the pinned version; a deliberately wrong checksum fails the build (checked once locally, not committed).

## 2. Image publication

- [x] 2.1 Add `.github/workflows/image.yml`: build on pull requests, build and push to `ghcr.io/<owner>/nextsre` on `main` and `v*` tags; tags `<sha>`, `main`, version; actions pinned by SHA; permissions `contents: read`, `packages: write`. Verify: `actionlint` passes locally, and the PR run builds without pushing.
- [x] 2.2 README: add an "Image" note (where the image is published, its tags). Verify: section present and matches the workflow's tags.

## 3. Shell harness narrowing

- [x] 3.1 `mcp_servers/shell_exec.py`: read `SHELL_BINARIES` at call time in the binary check; unset = built-in allowlist, set = intersection with it (empty intersection refuses everything); error message names the effective allowlist. Verify: new cases in `tests/test_harness.py` for each scenario of the `shell-and-http-hardening` delta (host tools refused, kubectl read passes and delete refused, `bash`/`curl` never added, empty refuses all, unset unchanged).
- [x] 3.2 `.env.example` and `DEEP.md` shell section: document `SHELL_BINARIES`. Verify: text present; `pytest` green.

## 4. Observe-only mode

- [x] 4.1 `agents/observer/observer_agent.py`: read `OBSERVE_ONLY` with `env_flag` inside `build_observer()`; split the system prompt into a shared part plus a remediation closing (current text) and an observe-only closing (recommend action, target, namespace and rollback; never claim an action was taken); omit `request_remediation` when on. Verify: new tests in `tests/test_privilege_boundary.py` — with `OBSERVE_ONLY=1` no tool is named `request_remediation` and the prompt has the observe-only closing; unset and `0` keep the tool and the delegate closing; `OBSERVE_ONLY=maybe` raises naming the variable.
- [x] 4.2 `.env.example`: document `OBSERVE_ONLY`. `DEEP.md` Observer section: describe both modes. Verify: text present; `pytest` green.

## 5. Gateway

- [x] 5.1 Fail closed: a startup check (called from `main()`) that raises naming `WEBHOOK_AUTH_TOKEN` when `DEMO_MODE=0` and neither token variable is set; demo mode unchanged. Verify: tests for real-mode-without-token raises, real-mode-with-token passes and a wrong bearer gets 401, demo-without-token passes.
- [x] 5.2 `OBSERVER_TIMEOUT_S`: parsed with `env_int` at startup (default 180) and passed to `call_agent` in `_dispatch_to_observer`. Verify: test that the dispatch calls `call_agent` with `timeout=900` when set, and that `OBSERVER_TIMEOUT_S=ten` fails the startup check naming the variable.
- [x] 5.3 Publish the outcome: after `call_agent`, emit an incident milestone (phase `hypothesis`, report as summary, service and namespace from the first firing alert); on exception or timeout emit phase `failed` with the error. Verify: tests patching `call_agent` and the notifier assert both cards and their service/namespace.
- [x] 5.4 Remediation-neutral prompt: replace "remediate if safe" (both the firing and the fallback text) with an investigate-and-diagnose instruction. Verify: test that the formatted prompt contains the quarantine block and no "remediat" wording.
- [x] 5.5 `.env.example` and `DEEP.md` gateway section: `WEBHOOK_AUTH_TOKEN` mandatory in real mode, `OBSERVER_TIMEOUT_S`, report sinks. Verify: text present; `pytest` green.

## 6. Deployment guide

- [x] 6.1 Write `docs/deploy-kubernetes.md` from design.md's contract for both modes: components, env table, Secrets, RBAC table (`k8s-ro`, `shell-exec`, `k8s-write`; no Secrets/ConfigMaps; tokens only on those three), `emptyDir` for `shell-exec`'s `$HOME/.kube`, single replica for `k8s-write` and `gateway`, NetworkPolicy matrix and egress needs, the enable-writes and rollback procedure (and why `REQUIRE_APPROVAL` stays off), cluster-side prerequisites (Alertmanager receiver with bearer token, Loki credential copy, Tempo policy rule, opt-in RoleBinding), and a manual smoke procedure (test alert through Alertmanager → report in gateway logs/notifier → trace in Tempo). In-cluster service names only; no addresses, hosts or personal paths. Verify: every variable the code reads in real/observe-only mode appears in the guide (grep `os.environ`/`env_*` call sites against it); personal-data grep on the file is empty.
- [x] 6.2 README "Switching to real backends": link the guide and mention observe-only mode and how writes are enabled. Verify: link resolves.

## 7. Whole-change verification

- [x] 7.1 `black . && isort . && flake8` clean, `pytest` green, `python -m tests.smoke_test` green. Verify: command output.

## 8. Review fixes (post-implementation check)

- [x] 8.1 Slack notifier escapes workload text (`&`, `<`, `>`) and caps the summary at 3000 characters; webhook/Slack failures log the host and exception type only. Verified by `tests/test_notifications_and_approvals.py`.
- [x] 8.2 Gateway: dedup window ≥ `OBSERVER_TIMEOUT_S`; non-positive timeout and stripped token handled at startup; card labels from the alert owning the incident id. Verified by `tests/test_gateway.py`.
- [x] 8.3 `OPERATOR_TIMEOUT_S` configurable and validated at Observer startup; observe-only A2A round-trip with no Operator. Verified by `tests/test_privilege_boundary.py`, `tests/test_incident_flow.py`.
- [x] 8.4 Shell harness denies kubectl's `-s` (`--server`) including clustered shorthands. Verified by `tests/test_harness.py`.
- [x] 8.5 Image: numeric `USER 10001:10001`; CI split into a smoke-tested `build` job and a push-only `publish` job, no cancellation on `main`. Verified by local build and `actionlint`.
- [x] 8.6 Guide: loopback in `MCP_ALLOWED_HOSTS`, `tcpSocket` probes, mandatory egress default-deny for `operator`/`shell-exec`, Alertmanager API lockdown, exact RBAC, `k8s-write` admission policy, gateway token mandatory; artifacts and docs name no specific cluster. Verified by reading the guide against the code.

## Workflow follow-up

- Cluster-side change: Alertmanager API lockdown, alerting rules and webhook receiver, Tempo NetworkPolicy rule, Loki credential copy, NextSRE namespace in the GitOps project, `RoleBinding` allowed in application namespaces behind an admission policy.
- Platform change: a NextSRE chart and its GitOps application (image from GHCR at a SHA tag), LLM gateway key, opt-in RoleBinding in the application chart, end-to-end smoke on the cluster.
- Follow-up change: wire `REQUIRE_APPROVAL` to a human (notifier card with token → gateway `/api/v1/approval` → resume or abort the Operator's paused tool).
- After merge: CI archives the change (`openspec-archive.yml`).
