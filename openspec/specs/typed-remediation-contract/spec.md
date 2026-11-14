# typed-remediation-contract Specification

## Purpose
Enforces a strongly typed Pydantic contract for remediation proposals and compensating rollback actions exchanged between the Observer and Operator agents over A2A.

## Requirements

### Requirement: Structured remediation proposal schema validation
The remediation bridge tool SHALL validate proposals against a Pydantic schema requiring action type, target namespace, target resource, parameters, and a pre-computed rollback action before sending to the Operator.

#### Scenario: Valid structured proposal is serialized and transmitted
- **WHEN** the Observer requests remediation with a valid action payload containing a target resource, namespace, and rollback action
- **THEN** the request passes validation and is dispatched to the Operator agent over A2A

#### Scenario: Malformed proposal payload is rejected locally
- **WHEN** the Observer submits a remediation proposal missing a target namespace or rollback action
- **THEN** validation fails immediately and returns a schema error to the Observer without contacting the Operator

### Requirement: Untrusted telemetry data quarantine and delimitation
Tools that read pod logs, events, and trace attributes SHALL delimit untrusted workload text inside structural XML tags to prevent indirect prompt injection from influencing agent decisions.

#### Scenario: Pod logs are wrapped in structural quarantine tags
- **WHEN** the Observer queries pod logs or warning events
- **THEN** the returned content is wrapped in quarantine tags with origin metadata and terminal escape sequences removed
