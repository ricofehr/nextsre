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

    # Reset the seed incident - but only when this process can actually see the
    # servers' state file. Under compose it lives on a volume this script does
    # not mount, so resetting here would rewrite a *different* file and report
    # success while the servers kept their post-remediation healthy state. That
    # lie costs an hour to debug, so say plainly when the reset was skipped.
    from mcp_servers import demo_state

    if "DEMO_STATE_FILE" not in os.environ:
        print(
            "• skipping demo-state reset: DEMO_STATE_FILE is unset, so this process\n"
            "  does not share the servers' state file (expected under docker compose).\n"
            "  The incident may already be resolved from a previous run.\n"
        )
    else:
        try:
            demo_state.reset()
            print("• demo state reset to seed incident (payments-api CrashLoopBackOff)\n")
        except OSError as exc:
            print(f"• could not reset demo state ({exc}); the servers keep their own\n")

    print(f"→ sending to Observer: {prompt}\n")

    def on_status(s: str) -> None:
        print(f"   {s}")

    report = await call_agent(OBSERVER_URL, prompt, on_status=on_status)
    print("\n================ INCIDENT REPORT ================\n")
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
