"""Test fixtures: isolate the demo state file.

Every test gets a fresh state file so no test can see another's mutations and
none of them can touch a real one. Backend selection reads the environment per
call, so setting ``DEMO_MODE`` here is enough - there is no cache to clear.
"""

from __future__ import annotations

import pytest

from mcp_servers import demo_state


@pytest.fixture(autouse=True)
def demo_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("DEMO_MODE", "1")
    demo_state.reset()
