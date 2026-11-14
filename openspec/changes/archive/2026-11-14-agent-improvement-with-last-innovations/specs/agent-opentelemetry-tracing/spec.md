# Spec Delta

## ADDED Requirements

### Requirement: GenAI semantic convention span attributes and incident audit trail
OpenTelemetry tracing SHALL record standardized GenAI attributes including agent role, system name, model identifier, and operational incident phase on spans, and capture milestone events on the root trace.

#### Scenario: Agent execution span records GenAI attributes
- **WHEN** an A2A agent or MCP tool processes an incident step with telemetry enabled
- **THEN** the active span attaches `gen_ai.system`, `gen_ai.agent.name`, and `gen_ai.operation.name` attributes

#### Scenario: Incident milestone records a span event
- **WHEN** an incident phase transition occurs (such as diagnosis or remediation)
- **THEN** a span event is added to the active trace with the phase name and summary metadata
