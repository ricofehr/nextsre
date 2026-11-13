# Tasks

## 1. Data Schemas and Quarantine Guardrails

- [x] 1.1 Create `common/schemas.py` defining Pydantic models for `RemediationProposal`, `RollbackAction`, and `AlertmanagerPayload` and verify unit tests validate valid and invalid payloads
- [x] 1.2 Implement `common/quarantine.py` to wrap untrusted logs/events in escaped structural XML tags and verify sanitization prevents escape tag injection

## 2. Ingestion Gateway

- [x] 2.1 Implement `services/gateway.py` with Starlette endpoint `POST /api/v1/alerts` that parses Alertmanager webhooks and forwards tasks to Observer via A2A
- [x] 2.2 Add deduplication cache in gateway by alert fingerprint and verify duplicate incoming alerts within window return HTTP 200 without duplicate agent tasks
- [x] 2.3 Add gateway service configuration to `docker-compose.yml` and verify container health endpoint

## 3. Typed Remediation and Circuit Breaker

- [x] 3.1 Update `agents/observer/remediation_tool.py` to validate proposals against `RemediationProposal` before sending to Operator and verify validation rejects missing fields
- [x] 3.2 Update `agents/operator/operator_agent.py` to parse structured proposals and record rollback context
- [x] 3.3 Implement `common/circuit_breaker.py` with sliding-window cooldown and per-incident action limits, integrate into `mcp_servers/k8s_write.py`, and verify cooldown blocks rapid repeated mutations

## 4. ChatOps Notifications and Interactive Approval

- [x] 4.1 Create `common/notifications/` with `Notifier` interface, `SlackBlockKitNotifier`, and `WebhookNotifier` emitting progressive incident cards
- [x] 4.2 Add interactive approval callback endpoint `POST /api/v1/approval` to gateway/app and verify approving token releases pending Operator execution
- [x] 4.3 Integrate notification dispatches into Observer and Operator incident milestones (triage, hypothesis, action, verification, resolution)

## 5. GenAI OpenTelemetry Semantic Conventions

- [x] 5.1 Update `common/telemetry.py` to record standard GenAI attributes (`gen_ai.system`, `gen_ai.agent.name`, `gen_ai.request.model`, `gen_ai.operation.name`) on agent and tool spans
- [x] 5.2 Add incident phase span events to the root trace and verify OTel export records attributes and events correctly

## 6. End-to-End Testing and Documentation

- [x] 6.1 Add integration tests covering webhook ingestion, structured remediation, circuit breaker cooldown, and notification dispatch
- [x] 6.2 Update `README.md` and `DEEP.md` with the new architecture, Alertmanager webhook setup, Slack notification variables, and circuit breaker configuration
