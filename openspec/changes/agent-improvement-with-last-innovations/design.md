# Design

## Context

NextSRE operates as an Observer/Operator agent pair over Google ADK and A2A 1.3.0, backed by 6 MCP servers with OpenTelemetry distributed tracing. See `proposal.md` for motivation. Currently, the system relies on manual CLI script execution (`fire_incident.py`), transfers unvalidated free-text actions across A2A, lacks anti-flapping controls on mutations, produces no external notifications, and lacks GenAI-specific OpenTelemetry attributes.

## Goals / Non-Goals

**Goals:**
- Provide an Alertmanager webhook receiver that converts firing alerts into structured Observer tasks.
- Establish a validated Pydantic schema for remediation proposals and rollback actions.
- Implement an untrusted telemetry quarantine wrapper for pod logs and events.
- Implement an in-memory sliding-window anti-flapping circuit breaker for Kubernetes write operations.
- Provide a modular notification engine supporting Slack Block Kit and generic JSON webhooks with progressive milestone streaming.
- Provide an interactive approval callback mechanism for `REQUIRE_APPROVAL=1`.
- Instrument agent spans with OpenTelemetry GenAI semantic conventions.

**Non-Goals:**
- Replacing Google ADK or the A2A 1.x protocol.
- Writing a full Kubernetes Operator CRD controller (the webhook gateway is sufficient for event-driven intake).
- Supporting cloud-proprietary alert formats (AWS CloudWatch, Datadog) directly; Alertmanager webhook format is the primary ingestion standard.

## Decisions

### Decision 1: Lightweight Starlette Webhook Gateway alongside Observer
- **Context**: Incoming alerts need an HTTP endpoint.
- **Decision**: Implement `services/gateway.py` as a lightweight Starlette service exposing `POST /api/v1/alerts`. It can run as an independent container or within the Observer process.
- **Rationale**: Reuses the Starlette and httpx dependencies already present without adding heavy web frameworks.
- **Alternatives considered**: Running a separate Celery/RabbitMQ queue (adds unnecessary infrastructure complexity for demo/SRE setups).

### Decision 2: Pydantic v2 Contract for A2A Remediation
- **Context**: Observer delegates write actions to Operator via `request_remediation`.
- **Decision**: Define `RemediationProposal` and `RollbackAction` in `common/schemas.py`. Observer serializes to JSON; Operator parses and validates before checking policy.
- **Rationale**: Guarantees type safety, catches missing parameters before network execution, and forces explicit specification of compensating rollback actions.
- **Alternatives considered**: Free text LLM interpretation (prone to hallucinated parameters and lack of deterministic validation).

### Decision 3: Sliding-Window State Store for Circuit Breakers
- **Context**: Rapid repeated mutations can trigger crashloops and cascading outages.
- **Decision**: Implement `CircuitBreaker` in `common/guardrails.py`, checking a thread-safe sliding window of timestamped actions per `(namespace, deployment)`. If an action occurred within `COOLDOWN_SECONDS` (default 300s), raise a `CircuitBreakerOpenError`.
- **Rationale**: Deterministic, zero-dependency in-process guardrail with optional shared file persistence in demo mode.
- **Alternatives considered**: External Redis cache (excessive overhead for lightweight deployments).

### Decision 4: Slack Block Kit Notification Formatter with Webhook Fallback
- **Context**: Notifications need to support both automated pipelines and human-in-the-loop ChatOps.
- **Decision**: Build `common/notifications/` with an abstract `Notifier` interface, implementing `SlackBlockKitNotifier` (rich blocks, buttons), `WebhookNotifier` (raw JSON), and `ConsoleNotifier` (stdout).
- **Rationale**: Allows seamless switching via environment variables (`SLACK_WEBHOOK_URL` vs `NOTIFICATION_WEBHOOK_URL`).

### Decision 5: OpenTelemetry GenAI Span Attributes
- **Context**: Tracing currently only records HTTP routes and tool names.
- **Decision**: Extend `common/telemetry.py` to record standard GenAI attributes (`gen_ai.system`, `gen_ai.agent.name`, `gen_ai.request.model`, `gen_ai.operation.name`) and add span events on incident phase transitions.
- **Rationale**: Aligns with OpenTelemetry GenAI semantic conventions used by modern observability backends (SigNoz, Datadog, Honeycomb, Tempo).

## Risks / Trade-offs

- **[Risk]** Circuit breaker cooldown might block legitimate rapid corrective changes.  
  → **Mitigation**: Allow human operator to override or bypass via explicit force parameter if approved.
- **[Risk]** Prompt quarantine delimiters could be stripped if workloads echo closing XML tags.  
  → **Mitigation**: Sanitize and escape XML closing tokens (`</telemetry_data>`) inside workload content before wrapping.
- **[Risk]** Webhook delivery retries from Alertmanager could trigger duplicate concurrent runs.  
  → **Mitigation**: Implement fingerprint-based deduplication with a 5-minute active incident lock.
