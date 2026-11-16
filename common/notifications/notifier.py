"""Progressive incident notifications and ChatOps dispatchers for NextSRE.

Supports:
- Console / logger output with structured milestone indicators
- Generic JSON webhook delivery
- Slack Block Kit formatted cards with interactive approval buttons
"""

from __future__ import annotations

import datetime
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from common.telemetry import record_incident_milestone_event

log = logging.getLogger("notifications")

IncidentPhase = Literal[
    "triage",
    "hypothesis",
    "remediation_proposed",
    "action_executing",
    "verification",
    "resolved",
    "failed",
]

_PHASE_EMOJI: dict[str, str] = {
    "triage": "🔍",
    "hypothesis": "💡",
    "remediation_proposed": "📋",
    "action_executing": "⚙️",
    "verification": "🧪",
    "resolved": "✅",
    "failed": "❌",
}


class IncidentCard(BaseModel):
    """Structured milestone notification card."""

    incident_id: str
    phase: IncidentPhase
    title: str
    summary: str
    service: str | None = None
    namespace: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    approval_token: str | None = None
    timestamp: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )


class BaseNotifier(ABC):
    """Abstract base class for incident notifiers."""

    @abstractmethod
    async def notify(self, card: IncidentCard) -> bool:
        """Dispatch an incident notification. Returns True on success."""


class ConsoleNotifier(BaseNotifier):
    """Prints formatted notification cards to stdout and logger."""

    async def notify(self, card: IncidentCard) -> bool:
        emoji = _PHASE_EMOJI.get(card.phase, "ℹ️")
        svc = f" [{card.namespace}/{card.service}]" if card.service else ""
        msg = (
            f"{emoji} [INCIDENT {card.incident_id}]{svc} "
            f"{card.phase.upper()}: {card.title} - {card.summary}"
        )
        if card.requires_approval:
            msg += f" (APPROVAL REQUIRED: token={card.approval_token})"
        print(msg)
        log.info(msg)
        return True


def _endpoint_for_log(url: str) -> str:
    """Host only: webhook URLs (Slack's included) carry their secret in the path."""
    return urlsplit(url).hostname or "<invalid url>"


class WebhookNotifier(BaseNotifier):
    """Dispatches raw JSON incident cards to an HTTP webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    async def notify(self, card: IncidentCard) -> bool:
        if not self.webhook_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(self.webhook_url, json=card.model_dump())
                return res.status_code in (200, 201, 202, 204)
        except Exception as exc:
            # The exception type only: httpx messages can embed the full URL.
            log.warning(
                "Failed to send webhook notification to %s: %s",
                _endpoint_for_log(self.webhook_url),
                type(exc).__name__,
            )
            return False


_SLACK_SECTION_TEXT_LIMIT = 3000
_SUMMARY_PREFIX = "*Summary:*\n"


def _slack_escape(text: str) -> str:
    """Neutralise Slack mrkdwn control sequences in untrusted text.

    Summaries carry Observer reports that quote workload logs; unescaped, a
    logged ``<!channel>`` or ``<https://evil|label>`` becomes a live ping or a
    disguised link in the incident channel. These three are the characters
    Slack documents as needing escaping.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_summary(summary: str) -> str:
    """The escaped summary, truncated to fit one Block Kit section."""
    budget = _SLACK_SECTION_TEXT_LIMIT - len(_SUMMARY_PREFIX)
    escaped = _slack_escape(summary)
    if len(escaped) > budget:
        escaped = escaped[: budget - 1] + "…"
    return _SUMMARY_PREFIX + escaped


class SlackBlockKitNotifier(BaseNotifier):
    """Dispatches Slack Block Kit formatted milestone cards."""

    def __init__(self, slack_webhook_url: str) -> None:
        self.slack_webhook_url = slack_webhook_url

    async def notify(self, card: IncidentCard) -> bool:
        if not self.slack_webhook_url:
            return False

        emoji = _PHASE_EMOJI.get(card.phase, "ℹ️")
        header_text = f"{emoji} NextSRE: {card.title}"
        if len(header_text) > 150:
            header_text = header_text[:147] + "..."

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text, "emoji": True},
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Incident ID:*\n`{_slack_escape(card.incident_id)}`",
                    },
                    {"type": "mrkdwn", "text": f"*Phase:*\n*{card.phase.upper()}*"},
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*Service:*\n{_slack_escape(card.service or 'N/A')} "
                            f"({_slack_escape(card.namespace or 'N/A')})"
                        ),
                    },
                    {"type": "mrkdwn", "text": f"*Time:*\n{card.timestamp[:19]}Z"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _slack_summary(card.summary)},
            },
        ]

        if card.requires_approval and card.approval_token:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Approve Remediation",
                                "emoji": True,
                            },
                            "style": "primary",
                            "value": card.approval_token,
                            "action_id": "approve_remediation",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                            "style": "danger",
                            "value": card.approval_token,
                            "action_id": "deny_remediation",
                        },
                    ],
                }
            )

        payload = {"blocks": blocks, "text": f"NextSRE [{card.incident_id}]: {card.title}"}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(self.slack_webhook_url, json=payload)
                return res.status_code == 200
        except Exception as exc:
            log.warning(
                "Failed to post Slack notification to %s: %s",
                _endpoint_for_log(self.slack_webhook_url),
                type(exc).__name__,
            )
            return False


class CompositeNotifier(BaseNotifier):
    """Fanned-out dispatcher across multiple configured notifiers."""

    def __init__(self, notifiers: list[BaseNotifier]) -> None:
        self.notifiers = notifiers

    async def notify(self, card: IncidentCard) -> bool:
        results = []
        for n in self.notifiers:
            try:
                results.append(await n.notify(card))
            except Exception as exc:
                log.error("Notifier %s failed: %s", n, exc)
                results.append(False)
        return any(results)


def get_notifier() -> CompositeNotifier:
    """Build composite notifier configured from environment."""
    notifiers: list[BaseNotifier] = [ConsoleNotifier()]

    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        notifiers.append(SlackBlockKitNotifier(slack_url))

    webhook_url = os.environ.get("NOTIFICATION_WEBHOOK_URL")
    if webhook_url:
        notifiers.append(WebhookNotifier(webhook_url))

    return CompositeNotifier(notifiers)


async def emit_incident_milestone(
    incident_id: str,
    phase: IncidentPhase,
    title: str,
    summary: str,
    *,
    service: str | None = None,
    namespace: str | None = None,
    details: dict[str, Any] | None = None,
    requires_approval: bool = False,
    approval_token: str | None = None,
) -> bool:
    """Convenience helper to emit an incident card to the active notifier."""
    card = IncidentCard(
        incident_id=incident_id,
        phase=phase,
        title=title,
        summary=summary,
        service=service,
        namespace=namespace,
        details=details or {},
        requires_approval=requires_approval,
        approval_token=approval_token,
    )
    # Record span event on current active OpenTelemetry span
    record_incident_milestone_event(phase, summary, details)
    notifier = get_notifier()
    return await notifier.notify(card)
