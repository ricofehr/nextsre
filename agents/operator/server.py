import uvicorn

from agents.a2a_app import ADKExecutor, build_app
from agents.operator.card import operator_card
from agents.operator.operator_agent import build_operator
from common.serving import bind_host, port

app = build_app(operator_card(), ADKExecutor(build_operator), service="operator")

if __name__ == "__main__":
    # Loopback by default. This is the write agent: anything that can reach it
    # drives remediation directly, with the read-only Observer bypassed.
    uvicorn.run(app, host=bind_host(), port=port(default=9301))
