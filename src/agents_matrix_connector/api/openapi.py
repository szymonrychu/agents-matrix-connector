"""Deterministic OpenAPI export: `python -m agents_matrix_connector.api.openapi > openapi.json`."""

import json

from agents_matrix_connector.api.app import create_api_app
from agents_matrix_connector.settings import ApiSettings


def export() -> str:
    """The api app's OpenAPI document as JSON with sorted keys and a trailing newline.

    Built without a database or an orchestrator: routes only close over their dependencies.
    """
    settings = ApiSettings(
        database_url="postgresql://openapi/export",
        orchestrator_url="http://openapi",
        orchestrator_token="openapi",
        homeserver_url="http://openapi",
        user_id="@openapi:openapi",
    )
    app = create_api_app(None, None, settings)  # type: ignore[arg-type]
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export(), end="")
