"""run_bot in-process against the fake homeserver and fake orchestrator on ASGI transports."""

import asyncio
import os
import signal
import socket

import httpx
import pytest
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.runtime import advisory_singleton

from agents_matrix_connector.bot import LOCK_NAME, run_bot
from agents_matrix_connector.db import directory
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.settings import BotSettings

BOT = "@agents:x"
TOKEN = "hs-secret-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_settings(url, port, **overrides) -> BotSettings:
    values = dict(
        database_url=url,
        orchestrator_url="http://orch",
        orchestrator_token="orch-token",
        homeserver_url="http://hs",
        user_id=BOT,
        access_token=TOKEN,
        http_host="127.0.0.1",
        http_port=port,
        poll_seconds=0.1,
        sync_timeout_seconds=1,
        shutdown_grace_seconds=5,
    )
    values.update(overrides)
    return BotSettings(**values)


@pytest.fixture
def port():
    return free_port()


@pytest.fixture
def fake_hs():
    return FakeHomeserver(BOT, TOKEN)


@pytest.fixture
def start(url, port, fake_hs):
    tasks = []

    def go(**overrides):
        settings = make_settings(url, port, **overrides)
        orch = FakeOrchestrator()
        task = asyncio.create_task(
            run_bot(
                settings,
                matrix_transport=httpx.ASGITransport(app=fake_hs.app),
                orchestrator_transport=httpx.ASGITransport(app=orch.app),
            )
        )
        tasks.append(task)
        return task

    yield go
    for task in tasks:
        task.cancel()


async def status(port: int, path: str) -> int | None:
    try:
        async with httpx.AsyncClient() as c:
            return (await c.get(f"http://127.0.0.1:{port}{path}", timeout=2)).status_code
    except httpx.TransportError:
        return None


async def wait_status(port: int, path: str, want: int, seconds: float = 15) -> None:
    async with asyncio.timeout(seconds):
        while await status(port, path) != want:
            await asyncio.sleep(0.05)


async def test_whoami_mismatch_exits_78(url, port, caplog):
    other = FakeHomeserver("@someone-else:x", TOKEN)
    code = await run_bot(
        make_settings(url, port),
        matrix_transport=httpx.ASGITransport(app=other.app),
        orchestrator_transport=httpx.ASGITransport(app=FakeOrchestrator().app),
    )
    assert code == 78
    assert BOT in caplog.text
    assert TOKEN not in caplog.text


async def test_readiness_and_auth_failure(pool, port, start, fake_hs, caplog):
    async with advisory_singleton(pool, LOCK_NAME):
        task = start()
        await wait_status(port, "/healthz", 200)
        assert await status(port, "/readyz") == 503
    await wait_status(port, "/readyz", 200)
    fake_hs.revoke_token()
    assert await asyncio.wait_for(task, 15) == 1
    assert "M_UNKNOWN_TOKEN" in caplog.text
    assert TOKEN not in caplog.text


async def test_singleton(pool, conn, port, start):
    async with advisory_singleton(pool, LOCK_NAME):
        task = start()
        await wait_status(port, "/healthz", 200)
        await asyncio.sleep(1.5)
        assert not task.done()
        assert await status(port, "/readyz") == 503
        assert await directory.get_sync_state(conn) == (None, None)
    await wait_status(port, "/readyz", 200)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 10) == 0


async def test_sigterm_while_waiting_for_the_lock(pool, port, start):
    async with advisory_singleton(pool, LOCK_NAME):
        task = start()
        await wait_status(port, "/healthz", 200)
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 5) == 0
