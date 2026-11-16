# incident-ingestion-gateway Specification

## Purpose
Provides an event-driven HTTP ingestion gateway that receives Prometheus Alertmanager webhook payloads and translates active alerts into autonomous A2A Observer investigation tasks.

## Requirements

### Requirement: Alertmanager webhook ingestion endpoint
The gateway service SHALL expose a `POST /api/v1/alerts` HTTP endpoint that parses Prometheus Alertmanager webhook payloads and triggers an Observer investigation task for firing alerts.

#### Scenario: Firing alert triggers an Observer investigation task
- **WHEN** Alertmanager sends a webhook POST to `/api/v1/alerts` containing firing alerts
- **THEN** the gateway extracts alert labels and annotations and dispatches an A2A task to the Observer URL

#### Scenario: Resolved alert notification is acknowledged without investigation
- **WHEN** Alertmanager sends a webhook POST where all alerts have status "resolved"
- **THEN** the gateway returns HTTP 200 and does not trigger an Observer investigation

### Requirement: Correlation and deduplication of repeated alert firings
The gateway service SHALL deduplicate repeated webhook firings for the same fingerprint and target service within an active incident window to prevent duplicate concurrent agent runs.

#### Scenario: Repeated alert for an active incident is deduplicated
- **WHEN** a webhook fires for an alert fingerprint currently under active investigation
- **THEN** the gateway acknowledges the webhook with HTTP 200 without launching a concurrent duplicate task

### Requirement: Webhook authentication is mandatory in real mode
With `DEMO_MODE=0`, the gateway SHALL refuse to start unless `WEBHOOK_AUTH_TOKEN` (or its alias `ALERTMANAGER_BEARER_TOKEN`) is set to a non-empty value. Surrounding whitespace in the configured token SHALL be ignored. In demo mode an unset token SHALL keep accepting unauthenticated webhooks, as before.

#### Scenario: Real mode without a token refuses to start
- **WHEN** the gateway starts with `DEMO_MODE=0` and neither `WEBHOOK_AUTH_TOKEN` nor `ALERTMANAGER_BEARER_TOKEN` is set
- **THEN** startup fails with an error naming `WEBHOOK_AUTH_TOKEN` and the server never listens

#### Scenario: Token from a file with a trailing newline still matches
- **WHEN** the gateway runs with `WEBHOOK_AUTH_TOKEN` set to the token followed by a newline, and a webhook carries the bare token as bearer
- **THEN** the gateway accepts the webhook

#### Scenario: Real mode with a token rejects a wrong bearer
- **WHEN** the gateway runs with `DEMO_MODE=0` and a token set, and a webhook arrives with a missing or different bearer token
- **THEN** the gateway responds HTTP 401 and dispatches nothing

### Requirement: Investigation report published through notifiers
When an Observer investigation dispatched by the gateway completes, the gateway SHALL publish the Observer's final report through the configured notifiers (console always, plus Slack and the generic webhook when configured), carrying the alert's service and namespace. When the dispatch fails or times out, the gateway SHALL publish a failure notification instead.

#### Scenario: Completed investigation is published
- **WHEN** the Observer returns a report for a dispatched alert
- **THEN** the notifiers receive an incident card containing that report, the alert's service and its namespace

#### Scenario: Failed investigation is published as a failure
- **WHEN** the Observer call raises or exceeds its timeout
- **THEN** the notifiers receive an incident card in the failed phase naming the error

### Requirement: Configurable Observer dispatch timeout
The gateway SHALL read `OBSERVER_TIMEOUT_S` as an integer number of seconds bounding each Observer call, defaulting to 180. A non-integer or non-positive value SHALL make the gateway fail at startup with an error naming the variable. Repeated firings of an alert SHALL stay deduplicated for at least as long as this timeout.

#### Scenario: Timeout override is honoured
- **WHEN** the gateway runs with `OBSERVER_TIMEOUT_S=900` and dispatches an alert
- **THEN** the Observer call is bounded by 900 seconds

#### Scenario: Malformed timeout refuses to start
- **WHEN** the gateway starts with `OBSERVER_TIMEOUT_S=ten`, `0` or `-1`
- **THEN** startup fails with an error naming `OBSERVER_TIMEOUT_S`

### Requirement: Remediation-neutral investigation prompt
The investigation prompt the gateway sends SHALL ask the Observer to investigate and diagnose the quarantined alerts and SHALL NOT itself instruct the Observer to remediate, so that the Observer's own instructions alone decide whether a remediation is requested or only recommended.

#### Scenario: Prompt does not order remediation
- **WHEN** the gateway formats the investigation prompt for a firing alert
- **THEN** the prompt contains the quarantined alert block and an instruction to investigate and diagnose, and no instruction to remediate
