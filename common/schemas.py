"""Strongly typed Pydantic data contracts for NextSRE.

Defines schemas for:
- Remediation proposals and compensating rollback actions between Observer and Operator
- Alertmanager webhook payloads ingested by the gateway
- Interactive approval tokens and status payloads
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RollbackAction(BaseModel):
    """Compensating action specification to revert a remediation if it fails."""

    action_type: Literal["restart", "scale", "command"]
    target_resource: str
    target_namespace: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class RemediationProposal(BaseModel):
    """Structured proposal sent by Observer to Operator over A2A."""

    action_type: Literal["restart", "scale", "command"]
    target_resource: str
    target_namespace: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str
    rollback_action: RollbackAction


class AlertItem(BaseModel):
    """Single alert entry within an Alertmanager payload."""

    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    fingerprint: str | None = None


class AlertmanagerPayload(BaseModel):
    """Prometheus Alertmanager webhook v4 payload."""

    version: str = "4"
    groupKey: str | None = None
    status: Literal["firing", "resolved"]
    receiver: str | None = None
    alerts: list[AlertItem] = Field(default_factory=list)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str | None = None
