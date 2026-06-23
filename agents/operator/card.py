import os

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol


def operator_card() -> AgentCard:
    url = os.environ.get("OPERATOR_PUBLIC_URL", "http://localhost:9301")
    return AgentCard(
        name="SRE Operator",
        description="Write-capable SRE agent. Executes guarded Kubernetes and host "
        "remediation actions behind a policy harness.",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding=TransportProtocol.JSONRPC)
        ],
        skills=[
            AgentSkill(
                id="remediate",
                name="Cluster remediation",
                description="Restart/scale deployments and run vetted host commands.",
                tags=["sre", "write", "kubernetes"],
                examples=[
                    "rolling-restart deployment payments-api in namespace payments",
                    "scale deployment web to 5 replicas in namespace web",
                ],
            )
        ],
    )
