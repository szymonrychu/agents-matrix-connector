"""Deterministic OpenAPI export of both apps.

uv run python -m agents_matrix_connector.api.openapi > openapi.json
uv run python -m agents_matrix_connector.api.openapi --agent > openapi-agent.json
"""

import argparse
import json

from fastapi import FastAPI

from agents_matrix_connector.api.agent import create_agent_app
from agents_matrix_connector.api.app import create_api_app
from agents_matrix_connector.settings import ApiSettings


def _settings() -> ApiSettings:
    return ApiSettings(
        database_url="postgresql://openapi/export",
        orchestrator_url="http://openapi",
        orchestrator_token="openapi",
        homeserver_url="http://openapi",
        user_id="@openapi:openapi",
    )


def _document(app: FastAPI) -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export() -> str:
    """The UI app's OpenAPI document as JSON with sorted keys and a trailing newline.

    Built without a database or an orchestrator: routes only close over their dependencies.
    """
    return _document(create_api_app(None, None, _settings()))  # type: ignore[arg-type]


def export_agent() -> str:
    """The agent listener's OpenAPI document (`/chat/v1`), in the same form as `export`."""
    return _document(create_agent_app(None, None, _settings()))  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="print an OpenAPI document of the connector")
    parser.add_argument("--agent", action="store_true", help="the agent listener's document")
    args = parser.parse_args(argv)
    print(export_agent() if args.agent else export(), end="")


if __name__ == "__main__":
    main()
