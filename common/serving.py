"""Shared serving wiring: where to bind.

Used by the MCP servers *and* both agent servers. None of these services
authenticates its caller, so reachability is the authorization model: anything
that can open a socket to shell-exec gets command execution, and anything that
can reach the Operator drives the write agent with the Observer bypassed.

The default is therefore loopback, and binding wider is a deliberate act -
docker-compose sets ``MCP_BIND_HOST=0.0.0.0`` because there the container network
is the isolation boundary. Running outside Docker (the README's per-terminal
path) stays on localhost.
"""

from __future__ import annotations

import os

from common.env import env_int

DEFAULT_BIND_HOST = "127.0.0.1"


def bind_host() -> str:
    return os.environ.get("MCP_BIND_HOST", DEFAULT_BIND_HOST)


def port(*, default: int) -> int:
    return env_int("PORT", default=default)
