# Proposal

## Why

NextSRE currently operates as a CLI-driven demo system where incidents are triggered manually via scripts, remediation actions are communicated as unvalidated free text strings between agents, mutations lack stateful rate-limiting or anti-flapping protection, notifications are restricted to terminal stdout, and OpenTelemetry spans do not capture GenAI operational attributes.

To align with modern autonomous SRE agent architecture and production-grade safety standards, NextSRE needs an event-driven Alertmanager ingestion gateway, strongly typed remediation contracts with rollback specifications, stateful circuit breakers against runaway mutations, interactive ChatOps notifications (Slack Block Kit / Webhooks), and GenAI-compliant telemetry.

## What Changes

- Add a production-ready **Alertmanager Ingestion Gateway** converting incoming webhook alerts into autonomous A2A Observer investigation tasks.
- Establish a **Typed Remediation Contract** with Pydantic models enforcing structured change proposals and pre-computed rollback actions between Observer and Operator.
- Add an **Input Quarantine Filter** protecting against indirect prompt injection from untrusted workload telemetry (logs, events, traces).
- Implement an **Anti-Flapping Circuit Breaker** in the Operator's mutation harness, enforcing action cooldowns, per-resource rate limits, and incident mutation budgets.
- Add a **ChatOps Notification Provider** supporting progressive incident milestone reporting (Slack Block Kit and generic webhooks) with interactive human approval gates.
- Extend **OpenTelemetry Instrumentation** with standardized GenAI semantic conventions (`gen_ai.system`, `gen_ai.agent.name`, `gen_ai.request.model`, `gen_ai.usage.*`) and structured timeline auditing.

## Capabilities

### New Capabilities
- `incident-ingestion-gateway`: Ingests Prometheus Alertmanager webhook payloads and converts them into structured A2A incident investigation requests.
- `typed-remediation-contract`: Validates structured remediation proposals and compensating rollback actions between Observer and Operator via Pydantic schemas.
- `remediation-circuit-breaker`: Enforces cooldown windows, flapping prevention, and incident-scoped mutation limits on write tools.
- `chatops-notifications`: Dispatches progressive incident milestone notifications and handles interactive human approval callbacks over Slack Block Kit and generic HTTP webhooks.

### Modified Capabilities
- `agent-opentelemetry-tracing`: Adds GenAI semantic convention attributes, agent role labeling, and incident timeline trace annotations to OpenTelemetry spans.

## Impact

- **Affected code**: `agents/observer/`, `agents/operator/`, `common/`, `mcp_servers/k8s_write.py`, `mcp_servers/shell_exec.py`, `docker-compose.yml`, `pyproject.toml`.
- **Dependencies**: Adds `pydantic>=2.0` (already a transitive dependency of adk/mcp, now explicit in core dependencies).
- **APIs**: New HTTP endpoint `/api/v1/alerts` on the gateway; new HTTP endpoint `/api/v1/notifications/actions` for interactive approvals; structured payload over the `/remediate` bridge.
- **Breaking changes**: None. Existing CLI `fire_incident.py` and demo mode remain supported.
