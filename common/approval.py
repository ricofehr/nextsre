"""Human-in-the-loop interactive approval management for NextSRE."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Literal

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0


class ApprovalRequest:
    """Represents a pending human-in-the-loop approval."""

    def __init__(
        self,
        token: str,
        incident_id: str,
        action_summary: str,
        timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        self.token = token
        self.incident_id = incident_id
        self.action_summary = action_summary
        self.timeout = timeout
        self.created_at = time.monotonic()
        self.decision: Literal["approved", "denied", "pending", "expired"] = "pending"
        self._event = asyncio.Event()

    async def wait(self) -> bool:
        """Wait until approved, denied, or timed out. Returns True if approved."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.timeout)
            return self.decision == "approved"
        except asyncio.TimeoutError:
            self.decision = "expired"
            return False

    def resolve(self, decision: Literal["approved", "denied"]) -> None:
        self.decision = decision
        self._event.set()


class ApprovalStore:
    """Thread-safe in-memory store for pending approval requests."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()

    async def create_request(
        self,
        incident_id: str,
        action_summary: str,
        timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> ApprovalRequest:
        token = secrets.token_urlsafe(16)
        req = ApprovalRequest(token, incident_id, action_summary, timeout=timeout)
        async with self._lock:
            self._requests[token] = req
        return req

    async def resolve(self, token: str, decision: Literal["approved", "denied"]) -> bool:
        async with self._lock:
            req = self._requests.get(token)
            if not req or req.decision != "pending":
                return False
            req.resolve(decision)
            return True

    def get_request(self, token: str) -> ApprovalRequest | None:
        return self._requests.get(token)

    def reset(self) -> None:
        self._requests.clear()


approval_store = ApprovalStore()
