"""The Observer's single bridge to action.

``request_remediation`` is an ordinary ADK ``FunctionTool`` from the LLM's point
of view, but under the hood it is an A2A client call to the Operator agent. It
is the ONLY tool the Observer has that can change anything - and it crosses a
process/network boundary into an agent with its own, independent harness.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from google.adk.tools import FunctionTool
from pydantic import ValidationError

from agents.a2a_app import call_agent
from common.env import env_int
from common.notifications import emit_incident_milestone
from common.schemas import RemediationProposal

log = logging.getLogger("remediation_tool")

DEFAULT_OPERATOR_URL = "http://localhost:9301"
DEFAULT_OPERATOR_TIMEOUT_S = 120


def _operator_url() -> str:
    return os.environ.get("OPERATOR_URL", DEFAULT_OPERATOR_URL)


def operator_timeout() -> int:
    return env_int("OPERATOR_TIMEOUT_S", default=DEFAULT_OPERATOR_TIMEOUT_S)


def validate_remediation_payload(raw_payload: str | dict[str, Any]) -> RemediationProposal:
    """Validate that the remediation proposal conforms to RemediationProposal schema."""
    if isinstance(raw_payload, str):
        try:
            data = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Remediation payload must be valid JSON matching RemediationProposal schema: {exc}"
            ) from exc
    elif isinstance(raw_payload, dict):
        data = raw_payload
    else:
        raise ValueError(f"Unsupported payload type: {type(raw_payload)}")

    return RemediationProposal.model_validate(data)


async def _request_remediation(action: str) -> str:
    """Delegate an infrastructure CHANGE to the operator agent.

    `action` must be a JSON string conforming to the RemediationProposal schema,
    specifying action_type, target_resource, target_namespace, reason, and rollback_action.
    """
    try:
        proposal = validate_remediation_payload(action)
    except (ValueError, ValidationError) as exc:
        log.warning("Rejected invalid remediation proposal: %s", exc)
        return (
            f"Error: Remediation proposal validation failed: {exc}. "
            "You must supply a valid JSON payload matching RemediationProposal with "
            "action_type, target_resource, target_namespace, reason, and rollback_action."
        )

    await emit_incident_milestone(
        incident_id="incident",
        phase="remediation_proposed",
        title=f"Proposed {proposal.action_type} on {proposal.target_resource}",
        summary=proposal.reason,
        service=proposal.target_resource,
        namespace=proposal.target_namespace,
        details={"rollback_action": proposal.rollback_action.model_dump()},
    )

    # Keep this below the caller's own budget (OBSERVER_TIMEOUT_S at the gateway):
    # this hop is nested inside the Observer's task, and with equal timeouts the
    # outer one always fires first, surfacing a slow-but-successful remediation
    # as a client timeout.
    serialized = proposal.model_dump_json()
    result = await call_agent(_operator_url(), serialized, timeout=operator_timeout())

    await emit_incident_milestone(
        incident_id="incident",
        phase="action_executing",
        title=f"Completed {proposal.action_type} on {proposal.target_resource}",
        summary=result[:150] if len(result) > 150 else result,
        service=proposal.target_resource,
        namespace=proposal.target_namespace,
    )

    return result


_request_remediation.__name__ = "request_remediation"
request_remediation = FunctionTool(_request_remediation)
