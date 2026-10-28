"""The agent listener (design D3): bearer gate, served paths and no source allowlist."""

import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from typing import cast

import httpx
import pytest
from agents_common.orchestrator import OrchestratorClient
from api_helpers import PREFIX, make_settings
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector.api.agent import CHAT_PREFIX, AgentGate, create_agent_app
from agents_matrix_connector.api.app import create_api_app

POD_PEER = ("10.0.0.5", 1234)


class _Conn:
    async def execute(self, query):
        return None


class _FakePool:
    """Enough of AsyncConnectionPool for /readyz."""

    @asynccontextmanager
    async def connection(self):
        yield _Conn()


def fake_pool() -> AsyncConnectionPool:
    return cast(AsyncConnectionPool, _FakePool())


def make_token() -> tuple[str, str]:
    token = secrets.token_hex(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def agent_client(digest: str, client=POD_PEER) -> httpx.AsyncClient:
    app = create_agent_app(fake_pool(), make_settings(agent_token_sha256=digest))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client), base_url="http://agents"
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_gate_accepts_only_the_hashed_token(caplog):
    caplog.set_level(logging.DEBUG)
    token, digest = make_token()
    other, _ = make_token()
    path = f"{CHAT_PREFIX}/openapi.json"
    refused = [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer   "},
        {"Authorization": f"Basic {token}"},
        {"Authorization": token},
        bearer(other),
        bearer(digest),
        bearer(token.upper()),
        bearer(token + "x"),
    ]
    bodies = []
    async with agent_client(digest) as c:
        for headers in refused:
            r = await c.get(path, headers=headers)
            assert r.status_code == 401, headers
            assert r.headers["www-authenticate"] == "Bearer"
            assert r.json()["error"]["code"] == "unauthorized"
            bodies.append(r.text)
        r = await c.post(f"{CHAT_PREFIX}/messages", json={"text": token}, headers=bearer(other))
        assert r.status_code == 401
        bodies.append(r.text)

        ok = await c.get(path, headers=bearer(token))
        assert ok.status_code == 200
        assert ok.json()["openapi"]
        lower = await c.get(path, headers={"authorization": f"bearer {token}"})
        assert lower.status_code == 200
        missing = await c.get(f"{CHAT_PREFIX}/nothing-here", headers=bearer(token))
        assert missing.status_code == 404
        bodies.append(missing.text)

    assert len({body for body in bodies[:-1]}) == 1  # one shared 401 body
    logged = caplog.text + " ".join(str(r.args) for r in caplog.records)
    for secret in (token, digest, other):
        assert secret not in logged
        for body in bodies:
            assert secret not in body


async def test_gate_closes_websockets_and_passes_lifespan():
    _, digest = make_token()
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    gate = AgentGate(inner, digest)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {}

    await gate({"type": "websocket", "path": f"{CHAT_PREFIX}/x", "headers": []}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    await gate({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


async def test_agent_app_paths_and_no_source_allowlist():
    token, digest = make_token()
    async with agent_client(digest) as c:
        for probe in ("/healthz", "/readyz"):
            r = await c.get(probe)
            assert r.status_code == 200, probe
            assert r.json() == {"status": "ok"}
        assert (await c.post("/healthz")).status_code == 401

        r = await c.get(f"{CHAT_PREFIX}/openapi.json", headers=bearer(token))
        assert r.status_code == 200
        assert all(
            p.startswith(f"{CHAT_PREFIX}/") or p in ("/healthz", "/readyz")
            for p in r.json()["paths"]
        )

        for other in (
            f"{PREFIX}/rooms",
            f"{PREFIX}/openapi.json",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/",
            "/chat/v2/x",
        ):
            r = await c.get(other, headers=bearer(token))
            assert r.status_code == 404, other
            assert r.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("peer", [POD_PEER, ("2001:db8::5", 1234), ("127.0.0.1", 1234)])
async def test_agent_app_admits_any_peer_with_the_token(peer):
    token, digest = make_token()
    async with agent_client(digest, client=peer) as c:
        r = await c.get(f"{CHAT_PREFIX}/openapi.json", headers=bearer(token))
        assert r.status_code == 200
        assert (await c.get(f"{CHAT_PREFIX}/openapi.json")).status_code == 401


async def test_ui_app_keeps_source_allowlist():
    token, digest = make_token()
    settings = make_settings(agent_token_sha256=digest)
    app = create_api_app(fake_pool(), cast(OrchestratorClient, None), settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=POD_PEER), base_url="http://api"
    ) as c:
        r = await c.get(f"{PREFIX}/rooms", headers=bearer(token))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"
        r = await c.get(f"{CHAT_PREFIX}/openapi.json", headers=bearer(token))
        assert r.status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="http://api"
    ) as c:
        for path in (f"{CHAT_PREFIX}/openapi.json", f"{CHAT_PREFIX}/messages"):
            r = await c.get(path, headers=bearer(token))
            assert r.status_code == 404, path
            assert r.json()["error"]["code"] == "not_found"
        assert not any(getattr(route, "path", "").startswith(CHAT_PREFIX) for route in app.routes)
