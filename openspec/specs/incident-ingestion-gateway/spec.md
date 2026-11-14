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
