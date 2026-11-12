"""README API coverage and the committed OpenAPI document (design D11, D14)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from api_helpers import make_settings
from fastapi.routing import APIRoute

from agents_matrix_connector import anonymize, policy, sender, sync, tracker, worker
from agents_matrix_connector.api import openapi
from agents_matrix_connector.api.agent import CHAT_PREFIX, create_agent_app
from agents_matrix_connector.api.app import PREFIX, create_api_app

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

PRESET_NOT_FOUND = "preset_not_found"  # recorded by agents_common.runs.dispatch


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


def agent_app_routes() -> set[str]:
    app = create_agent_app(None, None, make_settings())  # type: ignore[arg-type]
    out = set()
    for path, methods in _walk(app):
        if path.startswith(CHAT_PREFIX):
            out.update(f"{m} {path}" for m in methods - {"HEAD"})
    return out


def readme_routes(title: str = "API", prefix: str = PREFIX) -> set[str]:
    found = re.findall(
        rf"^### `(GET|POST|PATCH|PUT|DELETE) ({re.escape(prefix)}\S*)`\s*$",
        section(title),
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
        worker.NO_PRESET,
        tracker.SESSION_GONE,
        tracker.TURN_FAILED,
        tracker.TIMEOUT,
        tracker.EMPTY_REPLY,
        tracker.ALREADY_SENT,
        tracker.FORBIDDEN,
        tracker.MESSAGE_FAILED,
        tracker.MESSAGE_CANCELLED,
        tracker.TOO_LARGE_FOR_HOMESERVER,
        tracker.FILE_UNAVAILABLE,
        tracker.UPLOAD_FAILED,
        sender.FILES_NOT_ALLOWED,
        anonymize.PSEUDONYM_DISCLOSED,
        PRESET_NOT_FOUND,
    }
    assert policy.denied("<kind>") in README
    for reason in sorted(reasons):
        assert f"`{reason}`" in README, f"README does not name reason {reason}"
    for code in (
        "invalid_request",
        "invalid_rule",
        "invalid_template",
        "rule_exists",
        "unknown_preset",
        "not_found",
        "forbidden",
    ):
        assert f"`{code}`" in section("API"), f"README does not name error code {code}"
    assert "additive" in section("API")


def test_agent_openapi_committed_matches() -> None:
    committed = (ROOT / "openapi-agent.json").read_text(encoding="utf-8")
    assert committed == openapi.export_agent(), (
        "run: uv run python -m agents_matrix_connector.api.openapi --agent > openapi-agent.json"
    )
    assert committed.endswith("\n")
    paths = json.loads(committed)["paths"]
    documented = {f"{m.upper()} {p}" for p, ops in paths.items() for m in ops}
    assert {d for d in documented if d.split(" ")[1].startswith(CHAT_PREFIX)} == agent_app_routes()
    assert not any(p.startswith(CHAT_PREFIX) for p in json.loads(openapi.export())["paths"])
    for path in (f"{CHAT_PREFIX}/messages", f"{CHAT_PREFIX}/reactions"):
        parameters = {p["name"] for p in paths[path]["post"]["parameters"]}
        assert {"X-Orch-Session", "X-Orch-Slug"} <= parameters
    messages = paths[f"{CHAT_PREFIX}/messages"]["post"]
    assert {"404", "413"} <= set(messages["responses"])
    schemas = json.loads(committed)["components"]["schemas"]
    assert schemas["MessageIn"]["properties"]["files"]["maxItems"] == 4
    assert "files" in schemas["SendStatus"]["properties"]
    assert "agents_may_send_files" in schemas["ChatRoom"]["properties"]
    exported = subprocess.run(
        [sys.executable, "-m", "agents_matrix_connector.api.openapi", "--agent"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert exported.stdout == committed


def test_readme_lists_every_agent_route() -> None:
    expected = agent_app_routes()
    assert expected == {
        f"GET {CHAT_PREFIX}/rooms",
        f"POST {CHAT_PREFIX}/messages",
        f"POST {CHAT_PREFIX}/reactions",
        f"GET {CHAT_PREFIX}/messages/{{id}}",
        f"GET {CHAT_PREFIX}/notes",
        f"PUT {CHAT_PREFIX}/notes",
        f"GET {CHAT_PREFIX}/skills",
        f"GET {CHAT_PREFIX}/skills/{{name}}",
        f"PUT {CHAT_PREFIX}/skills/{{name}}",
        f"DELETE {CHAT_PREFIX}/skills/{{name}}",
        f"GET {CHAT_PREFIX}/admin/rooms",
        f"GET {CHAT_PREFIX}/admin/rooms/{{room_id}}",
        f"POST {CHAT_PREFIX}/admin/rooms/{{room_id}}/allow",
        f"PUT {CHAT_PREFIX}/admin/rooms/{{room_id}}/settings",
        f"GET {CHAT_PREFIX}/admin/users",
        f"GET {CHAT_PREFIX}/admin/rules",
        f"POST {CHAT_PREFIX}/admin/rules",
        f"DELETE {CHAT_PREFIX}/admin/rules/{{rule_id}}",
        f"POST {CHAT_PREFIX}/admin/room-requests",
        f"GET {CHAT_PREFIX}/admin/room-requests/{{request_id}}",
        f"PUT {CHAT_PREFIX}/internal/room-requests/{{request_id}}/decision",
        f"POST {CHAT_PREFIX}/internal/room-requests/{{request_id}}/confirm",
    }
    agent = section("Agent API")
    assert readme_routes("Agent API", CHAT_PREFIX) == expected
    for code in (
        "unauthorized",
        "not_found",
        "invalid_request",
        "invalid_key",
        "bad_reference",
        "room_required",
        "room_not_allowed",
        "room_not_authorised",
        "room_bound",
        "session_bound",
        "rate_limited",
        "room_chunk_limit",
        anonymize.PSEUDONYM_DISCLOSED,
        "invalid_slug",
        "unavailable",
        "room_unbound",
        "version_conflict",
        "notes_too_large",
        "skill_not_found",
        "skill_too_large",
        "skill_limit",
        "files_not_allowed",
        "file_not_found",
        "file_too_large",
        "invalid_file_path",
        "files_unavailable",
        "room_file_bytes_limit",
    ):
        assert f"`{code}`" in agent, f"README Agent API does not name error code {code}"
    for status in ("queued", "sending", "sent", "failed", "refused"):
        assert f"`{status}`" in agent, f"README Agent API does not name send status {status}"
    for reason in (
        "room_bound",
        "session_bound",
        "room_not_authorised",
        policy.ROOM_INFRASTRUCTURE,
        policy.ROOM_ENCRYPTED,
        policy.NOT_ALLOWED,
        policy.denied("<kind>"),
        tracker.FORBIDDEN,
        sender.FILES_NOT_ALLOWED,
        tracker.TOO_LARGE_FOR_HOMESERVER,
        tracker.FILE_UNAVAILABLE,
    ):
        assert f"`{reason}`" in agent, f"README Agent API does not name send reason {reason}"
    for phrase in (
        "X-Orch-Session",
        "X-Orch-Slug",
        "openapi-agent.json",
        "MATRIX_AGENT_RATE_PER_MINUTE",
        "MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR",
        "agents_may_initiate",
        "anonymize_names",
        "agents_may_send_files",
        "X-Orch-Agent",
        "MATRIX_AGENT_FILES_PER_SEND",
        "MATRIX_AGENT_IMAGE_MAX_BYTES",
        "MATRIX_AGENT_FILE_MAX_BYTES",
        "MATRIX_AGENT_ROOM_FILE_BYTES_PER_HOUR",
        ".attachments/",
    ):
        assert phrase in agent, phrase
    duplicate = re.search(r"^Duplicate rule:.*?(?=\n\n|\Z)", agent, re.MULTILINE | re.DOTALL)
    assert duplicate, "README Agent API has no paragraph starting 'Duplicate'"
    assert "60 s" in duplicate.group(0) and "existing" in duplicate.group(0)
    binding = re.search(r"^Binding rule:.*?(?=\n\n|\Z)", agent, re.MULTILINE | re.DOTALL)
    assert binding, "README Agent API has no paragraph starting 'Binding'"
    assert "1:1" in binding.group(0)
    assert "advisory" in agent
