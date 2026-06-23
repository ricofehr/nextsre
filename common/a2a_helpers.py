"""Tiny helpers around the a2a-sdk 1.x proto types.

The 1.x SDK exposes protobuf message types (``a2a_pb2``) rather than the old
pydantic models, so building a request and reading a streamed response is a
little verbose. These helpers keep that noise in one place.
"""

from __future__ import annotations

import uuid

from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
)


def user_text_request(text: str) -> SendMessageRequest:
    """Wrap a plain string as an A2A ``SendMessageRequest`` from the user."""
    return SendMessageRequest(
        message=Message(
            message_id=uuid.uuid4().hex,
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
        )
    )


def _parts_text(parts) -> str:
    return "".join(p.text for p in parts if p.text)


def render_event(event: StreamResponse) -> tuple[str, str]:
    """Turn one streamed event into ``(kind, text)`` for display/collection.

    ``kind`` is one of: ``status``, ``artifact``, ``message``, ``task``.
    """
    which = event.WhichOneof("payload")
    if which == "status_update":
        state = event.status_update.status.state
        msg = _parts_text(event.status_update.status.message.parts)
        # TaskState enum int -> readable suffix (TASK_STATE_WORKING -> working)
        from a2a.types import TaskState

        name = TaskState.Name(state).removeprefix("TASK_STATE_").lower()
        return "status", f"[{name}] {msg}".strip()
    if which == "artifact_update":
        art = event.artifact_update.artifact
        return "artifact", _parts_text(art.parts)
    if which == "message":
        return "message", _parts_text(event.message.parts)
    if which == "task":
        return "task", event.task.id
    return "unknown", ""
