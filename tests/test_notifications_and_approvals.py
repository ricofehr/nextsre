"""Unit tests for notifications and interactive approval gate."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from common.approval import approval_store
from common.notifications.notifier import (
    ConsoleNotifier,
    IncidentCard,
    SlackBlockKitNotifier,
)
from services.gateway import create_gateway_app


@pytest.mark.anyio
async def test_console_notifier():
    card = IncidentCard(
        incident_id="inc-1",
        phase="triage",
        title="Pod crashing",
        summary="OOMKilled pods",
        service="payments-api",
        namespace="payments",
    )
    notifier = ConsoleNotifier()
    success = await notifier.notify(card)
    assert success is True


@pytest.mark.anyio
async def test_slack_block_kit_payload(monkeypatch):
    card = IncidentCard(
        incident_id="inc-2",
        phase="remediation_proposed",
        title="Restart proposed",
        summary="Restarting deployment",
        service="payments-api",
        namespace="payments",
        requires_approval=True,
        approval_token="secret-token-123",
    )
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_payload.update(json.loads(request.read().decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    orig_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    notifier = SlackBlockKitNotifier("http://localhost:9999/slack")
    success = await notifier.notify(card)
    assert success is True
    assert "blocks" in captured_payload
    blocks = captured_payload["blocks"]
    assert any(b.get("type") == "header" for b in blocks)
    assert any(b.get("type") == "actions" for b in blocks)
    # Confirm action button carries approval token
    action_block = next(b for b in blocks if b.get("type") == "actions")
    assert action_block["elements"][0]["value"] == "secret-token-123"


@pytest.mark.anyio
async def test_approval_store_workflow():
    approval_store.reset()
    req = await approval_store.create_request("inc-3", "restart payments-api", timeout=1.0)
    assert req.decision == "pending"

    # Simulate approval
    async def approve_soon():
        await asyncio.sleep(0.05)
        await approval_store.resolve(req.token, "approved")

    asyncio.create_task(approve_soon())
    result = await req.wait()
    assert result is True
    assert req.decision == "approved"


def test_gateway_approval_endpoint():
    approval_store.reset()
    # Create request synchronously in event loop
    loop = asyncio.new_event_loop()
    req = loop.run_until_complete(
        approval_store.create_request("inc-4", "scale web to 3", timeout=10.0)
    )
    loop.close()

    client = TestClient(create_gateway_app(), base_url="http://localhost")
    # Call approval endpoint
    res = client.post("/api/v1/approval", json={"token": req.token, "decision": "approve"})
    assert res.status_code == 200
    assert res.json()["decision"] == "approved"

    # Repeat call fails (already resolved)
    res_repeat = client.post("/api/v1/approval", json={"token": req.token, "decision": "approve"})
    assert res_repeat.status_code == 404


def test_gateway_approval_invalid_body():
    client = TestClient(create_gateway_app(), base_url="http://localhost")
    # Non-dictionary payload returns 400
    res = client.post("/api/v1/approval", json=["not-a-dict"])
    assert res.status_code == 400
