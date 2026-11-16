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
from common.notifications import emit_incident_milestone

DEFAULT_OBSERVER_URL = "http://localhost:9300"
# Remediation-neutral, like the gateway's: the Observer's own prompt decides whether
# to delegate a fix or only recommend one (OBSERVE_ONLY).
DEFAULT_PROMPT = "Investigate the active incident and diagnose its root cause."


async def main() -> None:
    observer_url = os.environ.get("OBSERVER_URL", DEFAULT_OBSERVER_URL)
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
    await emit_incident_milestone(
        incident_id="incident-seed",
        phase="triage",
        title="Incident investigation started",
        summary=prompt,
        service="payments-api",
        namespace="payments",
    )

    def on_status(s: str) -> None:
        print(f"   {s}")

    report = await call_agent(observer_url, prompt, on_status=on_status)
    await emit_incident_milestone(
        incident_id="incident-seed",
        phase="resolved",
        title="Incident investigation completed",
        summary="Report emitted by Observer",
        service="payments-api",
        namespace="payments",
    )
    print("\n================ INCIDENT REPORT ================\n")
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
