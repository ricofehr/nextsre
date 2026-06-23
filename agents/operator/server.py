import os

import uvicorn

from agents.a2a_app import LangGraphExecutor, build_app
from agents.operator.card import operator_card
from agents.operator.operator_agent import build_operator

app = build_app(operator_card(), LangGraphExecutor(build_operator))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9301")))
