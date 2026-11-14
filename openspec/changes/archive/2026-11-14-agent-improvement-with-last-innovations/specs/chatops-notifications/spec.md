# Spec Delta

## Purpose
Provides a multi-channel incident notification pipeline dispatching progressive milestone status cards and handling interactive human approval callbacks over Slack Block Kit and webhooks.

## ADDED Requirements

### Requirement: Progressive incident milestone notifications
The notification engine SHALL dispatch structured milestone notifications for incident phases including triage, root-cause diagnosis, remediation proposal, verification, and resolution.

#### Scenario: Incident transition emits formatted status card
- **WHEN** the agent enters a new incident phase or completes a milestone
- **THEN** a notification payload formatted for Slack Block Kit or generic webhook is dispatched to configured endpoints

### Requirement: Interactive human-in-the-loop approval callback
When human approval is required (`REQUIRE_APPROVAL=1`), the notification engine SHALL emit an approval request with interactive buttons and pause execution until an authorized callback is received or timeout expires.

#### Scenario: Authorized human approval resumes Operator execution
- **WHEN** an operator clicks "Approve" via Slack or submits a valid approval callback to the notification endpoint
- **THEN** the approval gate validates the action token, unblocks the Operator, and executes the pending mutation

#### Scenario: Approval denial or timeout aborts remediation
- **WHEN** an operator clicks "Deny" or the approval timeout expires without a response
- **THEN** the mutation is aborted and the incident status is updated to rejected
