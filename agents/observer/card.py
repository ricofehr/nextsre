import os

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol


def observer_card() -> AgentCard:
    url = os.environ.get("OBSERVER_PUBLIC_URL", "http://localhost:9300")
    return AgentCard(
        name="SRE Observer",
        description="Read-only SRE agent. Investigates incidents across Kubernetes, "
        "Prometheus alerts and Elasticsearch logs, then delegates any fix.",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding=TransportProtocol.JSONRPC)
        ],
        skills=[
            AgentSkill(
                id="investigate",
                name="Incident investigation",
                description="Diagnose an incident from cluster state, alerts and logs; "
                "propose and (via the operator) apply a remediation.",
                tags=["sre", "read-only", "diagnosis"],
                examples=[
                    "investigate the active incident and remediate if safe",
                    "why are payments-api pods crashing?",
                ],
            )
        ],
    )
