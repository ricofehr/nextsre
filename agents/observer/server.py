import os

import uvicorn

from agents.a2a_app import LangGraphExecutor, build_app
from agents.observer.card import observer_card
from agents.observer.observer_agent import build_observer

app = build_app(observer_card(), LangGraphExecutor(build_observer))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9300")))
