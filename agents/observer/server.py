import uvicorn

from agents.a2a_app import ADKExecutor, build_app
from agents.observer.card import observer_card
from agents.observer.observer_agent import build_observer
from agents.observer.remediation_tool import operator_timeout
from common.env import env_flag
from common.serving import bind_host, port
from common.telemetry import init_telemetry

app = build_app(observer_card(), ADKExecutor(build_observer), service="observer")

if __name__ == "__main__":
    # The agent is built lazily on the first task; fail fast on malformed values.
    env_flag("OBSERVE_ONLY", default=False)
    operator_timeout()
    init_telemetry("observer")
    uvicorn.run(app, host=bind_host(), port=port(default=9300))
