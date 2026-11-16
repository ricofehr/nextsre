# Deploying NextSRE on Kubernetes

This is the contract a chart or a set of manifests has to satisfy to run NextSRE
against a real cluster. It does not ship a chart: the deployment belongs to the
platform layer that owns the cluster's namespaces, RBAC and secrets.

The worked example values assume a cluster running Prometheus with its Alertmanager in
`kube-monitoring`, Loki behind a basic-auth gateway in `kube-logcollect`, Tempo and an
OpenTelemetry collector in `kube-tracing`, and an OpenAI-compatible LLM gateway (e.g.
LiteLLM) in front of a local model. Replace them with your own service names.

Two modes, one image:

- **Observe-only** (start here): alerts in, evidence from the cluster, Prometheus, Loki
  and Tempo, a diagnosis and a *recommended* remediation out. Nothing deployed can
  change the cluster.
- **Writes enabled**: the Observer may delegate one remediation to the Operator, which
  restarts or scales a deployment in an explicitly allowed namespace. Writes are
  **autonomous** — see [Enabling writes](#enabling-writes) for what bounds them.

## Image

`ghcr.io/ricofehr/nextsre:<full commit sha>`, built by `.github/workflows/image.yml`.
Pin the SHA tag, not `main`. While the package is private, add an `imagePullSecret`.

Every component runs from the same image with a different command. The image runs as
UID/GID 10001 (numeric, so `runAsNonRoot: true` works without `runAsUser`) and works
with a read-only root filesystem.

## Components

| Component | Command | Port | Observe-only | Writes enabled |
|---|---|---|---|---|
| `k8s-ro` | `python -m mcp_servers.k8s_readonly` | 8101 | yes | yes |
| `prometheus-alerts` | `python -m mcp_servers.prometheus_alerts` | 8102 | yes | yes |
| `loki-logs` | `python -m mcp_servers.loki_logs` | 8103 | yes | yes |
| `tempo-traces` | `python -m mcp_servers.tempo_traces` | 8104 | yes | yes |
| `observer` | `python -m agents.observer.server` | 9300 | yes | yes |
| `gateway` | `python -m services.gateway` | 9302 | yes | yes |
| `operator` | `python -m agents.operator.server` | 9301 | — | yes |
| `k8s-write` | `python -m mcp_servers.k8s_write` | 8201 | — | yes |
| `shell-exec` | `python -m mcp_servers.shell_exec` | 8202 | — | yes |

Name each Service exactly as the component (`k8s-ro`, `observer`, …): every server
accepts its own name in the `Host` header, so in-namespace calls need no extra
`MCP_ALLOWED_HOSTS` entry. Do not deploy the write components in observe-only mode;
there is no reason to keep an idle write path.

**Probes:** use `tcpSocket` probes on the component's port. Every server validates the
`Host` header, and a kubelet `httpGet` probe sends the pod IP, which is refused with
400 (the pod would crash-loop). If you need an HTTP probe on the gateway, set
`httpHeaders: [{name: Host, value: gateway}]` on `/health`.

**Replicas:** one for `gateway` (the alert dedup cache is in memory) and one for
`k8s-write` (the circuit breaker is in memory; a restart resets its cooldowns). The
others are stateless.

## Environment

Set on every component:

| Variable | Value |
|---|---|
| `DEMO_MODE` | `0` |
| `MCP_BIND_HOST` | `0.0.0.0` (NetworkPolicies are the boundary, see below) |
| `PORT` | the component's port from the table above |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector.kube-tracing.svc.cluster.local:4317` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` |
| `OTEL_SERVICE_NAME` | one per component, e.g. `nextsre-observer` |

Per component:

| Component | Variable | Value |
|---|---|---|
| `prometheus-alerts` | `PROMETHEUS_URL` | `http://prometheus-server.kube-monitoring.svc.cluster.local` |
| | `ALERTMANAGER_URL` | `http://prometheus-alertmanager.kube-monitoring.svc.cluster.local:9093` |
| `loki-logs` | `LOKI_URL` | `http://loki-gateway.kube-logcollect.svc.cluster.local` |
| | `LOKI_USERNAME`, `LOKI_PASSWORD` | from a copy of Loki's basic-auth Secret (`loki-basic-auth` in `kube-logcollect`) |
| `tempo-traces` | `TEMPO_URL` | `http://tempo.kube-tracing.svc.cluster.local:3200` |
| `observer` | `OBSERVER_PUBLIC_URL` | `http://observer:9300` — callers send to the URL in the agent card, so this must be the in-cluster URL |
| | `OBSERVE_ONLY` | `1` to start, `0` to enable writes |
| | `K8S_RO_URL` | `http://k8s-ro:8101/mcp` |
| | `PROMETHEUS_ALERTS_URL` | `http://prometheus-alerts:8102/mcp` |
| | `LOKI_LOGS_URL` | `http://loki-logs:8103/mcp` |
| | `TEMPO_TRACES_URL` | `http://tempo-traces:8104/mcp` |
| | `OPERATOR_URL` | `http://operator:9301` (writes enabled) |
| | `OPERATOR_TIMEOUT_S` | per-read budget for the Operator call (default 120); raise it for a slow model, keeping it below `OBSERVER_TIMEOUT_S` (writes enabled) |
| `observer`, `operator` | `LLM_BASE_URL` | the LLM gateway's OpenAI-compatible endpoint, ending in `/v1` |
| | `LLM_MODEL` | the model id the gateway serves |
| | `LLM_API_KEY` | a key dedicated to NextSRE, from a Secret |
| `gateway` | `OBSERVER_URL` | `http://observer:9300` |
| | `WEBHOOK_AUTH_TOKEN` | from a Secret shared with Alertmanager. **Mandatory**, and so is `DEMO_MODE=0` on the gateway itself: the gateway refuses to start without a token only when `DEMO_MODE=0`, and demo is the default |
| | `OBSERVER_TIMEOUT_S` | `900` for a local model at 15-20 tok/s (default 180) |
| | `MCP_ALLOWED_HOSTS` | `127.0.0.1,localhost,gateway.<namespace>.svc,gateway.<namespace>.svc.cluster.local` — Alertmanager calls across namespaces. The value *replaces* the loopback default, so keep `127.0.0.1,localhost` (port-forwarded calls send them) |
| | `SLACK_WEBHOOK_URL`, `NOTIFICATION_WEBHOOK_URL` | optional: where reports are published (they always go to the log) |
| `operator` | `OPERATOR_PUBLIC_URL` | `http://operator:9301` |
| | `K8S_WRITE_URL`, `SHELL_URL` | `http://k8s-write:8201/mcp`, `http://shell-exec:8202/mcp` |
| | `REQUIRE_APPROVAL` | `0` — the confirmation gate is not wired to a human yet; `1` would stall every remediation |
| `k8s-write` | `ALLOWED_NAMESPACES` | the namespaces that have a `k8s-write` RoleBinding, comma-separated; empty refuses every write |
| | `MAX_REPLICAS` | scale ceiling (default 20) |
| | `REMEDIATION_COOLDOWN_SECONDS`, `MAX_INCIDENT_MUTATIONS` | circuit breaker: per-deployment cooldown (default 300) and per-incident budget (default 5); leave `ENABLE_CIRCUIT_BREAKER` unset (on) |
| `shell-exec` | `SHELL_BINARIES` | `kubectl` — `df`, `free`, `systemctl`, … would describe the pod, not a host |

Reports may quote log lines; only configure Slack if those can leave the cluster.

**Filesystem:** with a read-only root filesystem, mount an `emptyDir` on `/tmp` for
every component and on `/home/sre/.kube` for `shell-exec` (kubectl's discovery cache).

## RBAC

Only the three components that talk to the Kubernetes API get a ServiceAccount token;
every other pod sets `automountServiceAccountToken: false`.

| ServiceAccount | Rules (namespaced Role) | Bound in |
|---|---|---|
| `k8s-ro` | `list` on `pods` and `events`, `get` on `pods/log` | every watched namespace |
| `shell-exec` | `get`, `list`, `watch` on `pods`, `pods/log`, `events`, `services`, `endpoints`, `apps/deployments`, `apps/replicasets`, `apps/statefulsets`; `get`, `list` on `metrics.k8s.io/pods` | every watched namespace |
| `k8s-write` | `patch` on `apps/deployments` and `apps/deployments/scale` | the write namespaces only |

These are exactly the calls the code makes (`k8s-write` never reads). No rule grants
Secrets or ConfigMaps, and the shell harness also refuses `secret` tokens. Do not use the
built-in `view` ClusterRole: it adds ConfigMaps and every other namespaced resource type.
Note that `shell-exec` does read workload specs (`kubectl get … -o yaml`), so a value
written inline in a pod's `env` is readable by the Operator's model and can end up in a
report: keep credentials in Secrets. A namespace without a binding answers 403, which
the tool reports to the agent as an error.

`patch` on a Deployment reaches the whole pod template, not only the replica count and
the restart annotation (`sre-agent/restartedAt`) the tools set. Whoever holds the
`k8s-write` token could swap the image or the ServiceAccount. Close that with an
admission policy on that subject (a sketch; dry-run it on your cluster before relying
on it):

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: nextsre-k8s-write-scope
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: ["apps"]
        apiVersions: ["v1"]
        operations: ["UPDATE"]
        resources: ["deployments"]
  matchConditions:
    - name: k8s-write-only
      expression: request.userInfo.username == "system:serviceaccount:<namespace>:k8s-write"
  variables:
    - name: newAnnotations
      expression: >-
        has(object.spec.template.metadata.annotations) ?
        object.spec.template.metadata.annotations : {}
    - name: oldAnnotations
      expression: >-
        has(oldObject.spec.template.metadata.annotations) ?
        oldObject.spec.template.metadata.annotations : {}
  validations:
    - expression: >-
        object.spec.template.spec == oldObject.spec.template.spec &&
        object.spec.selector == oldObject.spec.selector &&
        object.spec.strategy == oldObject.spec.strategy &&
        object.spec.template.metadata.labels == oldObject.spec.template.metadata.labels &&
        variables.newAnnotations.filter(k, k != "sre-agent/restartedAt").all(k,
          k in variables.oldAnnotations &&
          variables.oldAnnotations[k] == variables.newAnnotations[k]) &&
        variables.oldAnnotations.filter(k, k != "sre-agent/restartedAt").all(k,
          k in variables.newAnnotations)
      message: k8s-write may only change replicas and the sre-agent/restartedAt annotation
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: nextsre-k8s-write-scope
spec:
  policyName: nextsre-k8s-write-scope
  validationActions: ["Deny"]
```

Scaling goes through the `deployments/scale` subresource, which only carries a replica
count, so the policy does not need to match it.

On a GitOps platform, bind the platform namespaces from the NextSRE chart, and let each
application opt in from its own chart with two values: *read* (binds `k8s-ro` and
`shell-exec`) and *write* (binds `k8s-write`). Restrict those RoleBindings with an
admission policy to the three NextSRE subjects and their Roles.

## NetworkPolicies

None of these services authenticates its caller: reachability is the authorization
model. Apply a default-deny ingress policy to the NextSRE namespace, then allow:

| To | From | Port |
|---|---|---|
| `gateway` | Alertmanager pods (`kube-monitoring`) | 9302 |
| `observer` | `gateway` | 9300 |
| `k8s-ro`, `prometheus-alerts`, `loki-logs`, `tempo-traces` | `observer` | 8101-8104 |
| `operator` | `observer` | 9301 |
| `k8s-write`, `shell-exec` | `operator` | 8201, 8202 |

Your CNI must enforce NetworkPolicy; on one that does not, any pod can drive
`operator:9301` or `shell-exec:8202` and these tables are decoration.

**Egress:** default-deny egress is **mandatory** for `operator` and `shell-exec` once
writes are enabled: they act on text that originates from workloads, and the harness is
not an exfiltration barrier. Allow only:

| From | To |
|---|---|
| `operator` | `k8s-write`, `shell-exec`, the LLM gateway, the OTel collector, DNS |
| `shell-exec`, `k8s-write` | the kube-apiserver, the OTel collector, DNS |

Recommended for the rest: `k8s-ro` → kube-apiserver; `prometheus-alerts` → Prometheus
and Alertmanager; `loki-logs` → the Loki gateway; `tempo-traces` → Tempo; `observer` →
the four read servers, `operator`, the LLM gateway; `gateway` → `observer`, the
notification endpoints; all → OTel collector and DNS.

On the observability side, check the backends' own policies: Tempo's query port
(3200) is often limited to Grafana's namespace and must also admit `tempo-traces`.

## Cluster-side prerequisites

1. **Alertmanager's API locked down.** The webhook token proves the caller is
   Alertmanager, not where an alert came from: Alertmanager accepts `POST /api/v2/alerts`
   from any client, so any pod that reaches `:9093` can make NextSRE investigate (and,
   with writes on, act on) whatever it likes. Restrict ingress to Alertmanager to
   Prometheus and `prometheus-alerts`, and route to the `nextsre` receiver with
   matchers on your own alert rules.
2. **Alerting rules** in Prometheus (crash-looping pods, pods not ready, error rate…):
   with no rules, Alertmanager never calls the gateway.
3. **An Alertmanager webhook receiver** routed to the alerts NextSRE should investigate:

   ```yaml
   receivers:
     - name: nextsre
       webhook_configs:
         - url: http://gateway.<namespace>.svc.cluster.local:9302/api/v1/alerts
           send_resolved: false
           http_config:
             authorization:
               type: Bearer
               credentials_file: /etc/alertmanager/secrets/nextsre/token
   ```
4. **Loki credentials** copied into the NextSRE namespace.
5. **Tempo's NetworkPolicy** admitting `tempo-traces` on 3200.
6. **An LLM gateway key** for NextSRE.
7. **RoleBindings** for the watched (and later, write) namespaces, as above.

## Smoke test

1. Port-forward the gateway and send a test alert with the token:

   ```bash
   kubectl -n <namespace> port-forward svc/gateway 9302:9302 &
   curl -s -X POST http://127.0.0.1:9302/api/v1/alerts \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"status":"firing","alerts":[{"status":"firing","labels":{"alertname":"SmokeTest","service":"<deployment>","namespace":"<watched namespace>","severity":"warning"},"annotations":{"summary":"smoke test"},"fingerprint":"smoke-1"}]}'
   ```

   Expect `202 accepted`. Without the header, expect `401`.
2. Follow `kubectl logs deploy/gateway`: an `Investigation report` card arrives once
   the Observer finishes (or a `failed` card naming the error).
3. In Tempo, search `resource.service.name = "nextsre-gateway"`: the trace spans
   gateway → observer → MCP servers → backends.
4. Then fire a real alert end to end through Alertmanager (e.g. scale a test deployment
   to an image that crash-loops) and check the report names the right pod and cause.

## Enabling writes

Writes need two independent keys, so a mistake in one still refuses the write:

1. Deploy `operator`, `k8s-write` and `shell-exec` with the environment, RBAC and
   NetworkPolicies above.
2. List the target namespaces in `ALLOWED_NAMESPACES` **and** create the `k8s-write`
   RoleBinding in each of them.
3. Set `OBSERVE_ONLY=0` on the Observer.

What bounds an autonomous write:

- the tools themselves: rolling restart or scale of a Deployment, nothing else;
- protected namespaces (`kube-system`, `kube-public`, `kube-node-lease`, `default`),
  the namespace allowlist, RFC 1123 names, a replica floor of 1 (`ALLOW_SCALE_TO_ZERO`
  must stay unset) and `MAX_REPLICAS`;
- the circuit breaker: a cooldown per deployment and a mutation budget per incident;
- `shell-exec` is read-only (`kubectl` read verbs only, a denylist that also blocks
  redirecting it to another API server, read-only RBAC, mandatory egress default-deny);
- the Operator re-evaluates the Observer's proposal under its own prompt and harness.

Run in observe-only mode long enough to compare the recommended remediations with what
a human would have done before taking this step.

**Rollback:** `OBSERVE_ONLY=1` (the Observer loses the bridge at its next start) or an
empty `ALLOWED_NAMESPACES` (every write refused). Removing the RoleBindings is the
cluster-side equivalent.

When the kubectl client falls more than one minor behind the cluster, rebuild with a
newer `KUBECTL_VERSION` and its checksums (see the `Dockerfile`).
