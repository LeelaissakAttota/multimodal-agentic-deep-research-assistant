"""Run the deep-research control API on 127.0.0.1 (never a public interface)."""

from __future__ import annotations

import argparse

import uvicorn

from deep_research.assistant.store import AssistantStore
from deep_research.control.api import build_control_app
from deep_research.control.service import DeepResearchControl, admin_token_from_env
from deep_research.core.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Deep Research supervised-control API")
    parser.add_argument("--port", type=int, default=8711)
    args = parser.parse_args()
    control = DeepResearchControl(settings)
    app = build_control_app(control.manager, control.secrets, admin_token=admin_token_from_env(), title="deep-research",
                            assistant=AssistantStore(control.assistant_db))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
