import uvicorn

from agents.a2a_app import LangGraphExecutor, build_app
from agents.observer.card import observer_card
from agents.observer.observer_agent import build_observer
from common.serving import bind_host, port

app = build_app(observer_card(), LangGraphExecutor(build_observer))

if __name__ == "__main__":
    uvicorn.run(app, host=bind_host(), port=port(default=9300))
