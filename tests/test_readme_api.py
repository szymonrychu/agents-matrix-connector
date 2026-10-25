"""README API coverage and the committed OpenAPI document (design D11, D14)."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from fastapi.routing import APIRoute

from agents_matrix_connector import policy, sync, tracker, worker
from agents_matrix_connector.api import openapi
from agents_matrix_connector.api.app import PREFIX, create_api_app
from api_helpers import make_settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

# worker and tracker record these as literals in their own modules (and `preset_not_found` in
# agents_common.runs); the test also checks the literal is still used where it is recorded.
WORKER_REASONS = ("no_preset", "session_gone")
TRACKER_REASONS = (
    "session_gone",
    "message_failed",
    "message_cancelled",
    "turn_failed",
    "timeout",
    "empty_reply",
    "forbidden",
)
COMMON_REASONS = ("preset_not_found",)


def section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _walk(router, prefix: str = ""):
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route.methods
        elif hasattr(route, "original_router"):
            yield from _walk(route.original_router, prefix + route.include_context.prefix)


def app_routes() -> set[str]:
    app = create_api_app(None, None, make_settings())  # type: ignore[arg-type]
    out = set()
    for path, methods in _walk(app):
        if path.startswith(PREFIX):
            out.update(f"{m} {path}" for m in methods - {"HEAD"})
    return out


def readme_routes() -> set[str]:
    found = re.findall(
        rf"^### `(GET|POST|PATCH|PUT|DELETE) ({re.escape(PREFIX)}\S*)`\s*$",
        section("API"),
        re.MULTILINE,
    )
    return {f"{m} {p}" for m, p in found}


def test_openapi_committed_matches() -> None:
    committed = (ROOT / "openapi.json").read_text(encoding="utf-8")
    assert committed == openapi.export(), (
        "run: uv run python -m agents_matrix_connector.api.openapi > openapi.json"
    )
    assert committed.endswith("\n")


def test_readme_lists_every_route() -> None:
    expected = app_routes()
    assert len(expected) >= 11
    api = section("API")
    assert PREFIX in api
    assert readme_routes() == expected


def test_readme_lists_every_reason() -> None:
    reasons = {
        policy.ROOM_INFRASTRUCTURE,
        policy.ROOM_ENCRYPTED,
        policy.NOT_ALLOWED,
        sync.UNSUPPORTED_CONTENT,
        sync.NOT_MENTIONED,
        sync.QUEUE_FULL,
        *WORKER_REASONS,
        *TRACKER_REASONS,
        *COMMON_REASONS,
    }
    for module, names in ((worker, WORKER_REASONS), (tracker, TRACKER_REASONS)):
        source = inspect.getsource(module)
        for name in names:
            built = name.startswith("message_") and 'f"message_{' in source
            assert name in source or built, f"{name} gone from {module}"
    assert policy.denied("<kind>") in README
    for reason in sorted(reasons):
        assert f"`{reason}`" in README, f"README does not name reason {reason}"
    for code in (
        "invalid_request",
        "invalid_rule",
        "rule_exists",
        "unknown_preset",
        "not_found",
        "forbidden",
    ):
        assert f"`{code}`" in section("API"), f"README does not name error code {code}"
    assert "additive" in section("API")
