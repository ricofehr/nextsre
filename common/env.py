"""Fail-closed environment parsing.

``DEMO_MODE`` decides whether the MCP servers talk to synthetic state or to real
Kubernetes/Prometheus/Loki/Tempo and run real subprocesses. A comparison like
``os.environ.get("DEMO_MODE", "1") == "1"`` treats every value it does not
recognise - ``true``, ``yes``, a stray space - as "not demo", which silently arms
the real backends. That is the wrong direction to fail in, so ``env_flag``
accepts the usual spellings and rejects anything else loudly.
"""

from __future__ import annotations

import os

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_flag(name: str, *, default: bool) -> bool:
    """Read ``name`` as a boolean, raising on an unrecognised value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(
        f"{name}={raw!r} is not a boolean; use one of {sorted(_TRUE)} or {sorted(_FALSE)}"
    )


def env_int(name: str, *, default: int) -> int:
    """Read ``name`` as an int, naming the variable when it is malformed.

    A bare ``int(os.environ[...])`` raises mid-incident from inside a tool with a
    message that does not say which setting is wrong.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None


def env_csv(name: str, *, default: str) -> set[str]:
    """Read ``name`` as a comma-separated set, dropping blanks and whitespace.

    Unstripped parsing turns a natural ``"web, payments"`` into entries that
    match nothing, and a trailing comma into an empty-string entry that matches
    an empty namespace.
    """
    raw = os.environ.get(name, default)
    return {item.strip() for item in raw.split(",") if item.strip()}
