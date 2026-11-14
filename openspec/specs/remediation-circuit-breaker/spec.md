# remediation-circuit-breaker Specification

## Purpose
Enforces stateful rate-limiting, anti-flapping cooldown windows, and per-incident action quotas on write tools to prevent runaway or oscillating infrastructure mutations.

## Requirements

### Requirement: Per-resource anti-flapping cooldown
The write tools SHALL reject mutations targeting a workload that has undergone a remediation action within an active cooldown window to prevent rapid mutation oscillation.

#### Scenario: Mutation within cooldown window is rejected
- **WHEN** a restart or scale operation is requested on a deployment that was mutated less than the cooldown period ago
- **THEN** the write tool raises a circuit breaker error and refuses to execute the mutation

#### Scenario: Mutation after cooldown expiration succeeds
- **WHEN** a mutation is requested on a deployment after its previous cooldown window has elapsed
- **THEN** the write tool executes the mutation and resets the resource's cooldown timer

### Requirement: Incident-scoped mutation budget ceiling
The Operator harness SHALL enforce a maximum budget of write mutations per incident session and block further mutations once the budget is exhausted.

#### Scenario: Mutation budget exhaustion triggers escalation
- **WHEN** the number of write mutations within a single incident session reaches the configured budget ceiling
- **THEN** subsequent write requests are denied with an error instructing escalation to human engineers
