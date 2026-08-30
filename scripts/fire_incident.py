"""Fire a seed incident at the Observer and stream its progress.

Usage:
    python -m scripts.fire_incident
    python -m scripts.fire_incident "why are payments-api pods crashing?"

Resets the synthetic cluster to the OOMKill incident, then sends an
investigation request to the Observer over A2A and prints status updates as
they stream back, followed by the final incident report.
"""

from __future__ import annotations

import asyncio
import os
import sys

from agents.a2a_app import call_agent

OBSERVER_URL = os.environ.get("OBSERVER_URL", "http://localhost:9300")
DEFAULT_PROMPT = "Investigate the active incident and remediate it if it is safe to do so."


async def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT

    # Reset the shared demo state to the seed incident. Only works when this
    # process can see the same state file as the MCP servers - under compose the
    # file lives on a volume the servers share and this script does not, so a
    # missing/unwritable path is expected rather than an error.
    from mcp_servers import demo_state

    try:
        demo_state.reset()
        print("• demo state reset to seed incident (payments-api CrashLoopBackOff)\n")
    except OSError as exc:
        print(f"• skipping demo-state reset ({exc}); the servers keep their own state\n")

    print(f"→ sending to Observer: {prompt}\n")

    def on_status(s: str) -> None:
        print(f"   {s}")

    report = await call_agent(OBSERVER_URL, prompt, on_status=on_status)
    print("\n================ INCIDENT REPORT ================\n")
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
