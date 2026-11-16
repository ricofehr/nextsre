# Spec Delta

## Purpose
Lets NextSRE run on a real cluster with no path to any mutation: the Observer investigates and reports a diagnosis and a recommended remediation, and nothing in the deployment can execute one.

## ADDED Requirements

### Requirement: Observe-only switch
The Observer SHALL read `OBSERVE_ONLY` as a strict boolean (accepting `1/0/true/false/yes/no/on/off`, case-insensitive) when the agent is built. Unset SHALL mean `false`. Any other value SHALL make the Observer fail at startup with an error naming the variable.

#### Scenario: Malformed value refuses to start
- **WHEN** the Observer is built with `OBSERVE_ONLY=maybe`
- **THEN** building the agent raises an error naming `OBSERVE_ONLY` and no server starts

#### Scenario: Unset or false keeps the remediation bridge
- **WHEN** the Observer is built with `OBSERVE_ONLY` unset or set to `0`
- **THEN** its tool surface includes `request_remediation` and its instructions keep the delegate-then-verify closing, as before this change

### Requirement: No remediation tool in observe-only mode
With `OBSERVE_ONLY` true, the Observer SHALL expose only its read tools (Kubernetes read, Prometheus/Alertmanager, Loki, Tempo) and SHALL NOT expose `request_remediation` or any other tool that reaches the Operator. It SHALL NOT require `OPERATOR_URL` or a reachable Operator.

#### Scenario: Tool surface excludes the remediation bridge
- **WHEN** the Observer is built with `OBSERVE_ONLY=1`
- **THEN** its tools are the four read MCP toolsets and no tool named `request_remediation` is present

#### Scenario: Observer serves without an Operator
- **WHEN** the Observer runs with `OBSERVE_ONLY=1` and no Operator is deployed
- **THEN** it accepts and completes investigation tasks without attempting any connection to an Operator

### Requirement: Recommendation instead of delegation
With `OBSERVE_ONLY` true, the Observer's instructions SHALL state that it cannot request any change, and SHALL ask it to end with an incident report containing the symptom, the root-cause hypothesis with its evidence, and a recommended remediation (action, target resource and namespace, rollback) for a human to apply.

#### Scenario: Report carries a recommendation, not an action
- **WHEN** the Observer completes an investigation in observe-only mode
- **THEN** its instructions direct a final report with symptom, root cause, evidence and a recommended remediation, and never claim that an action was taken
