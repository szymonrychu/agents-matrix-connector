"""Canary: a marker in message bodies and media names reaches nowhere it does not belong.

Covers the history routes (agent and admin), the attachment routes and one fetcher tick: the
marker may appear only in the 200 history bodies of the non-anonymized room (it is the message
text there), never in a log line, a non-2xx body, or a column of the request and crawl tables.
"""

import base64
import json
import logging
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import test_api_agent as rig
from agents_common.fake_orchestrator import FakeOrchestrator
from api_helpers import make_settings
from history_helpers import add_m
from test_agent_gate import bearer, make_token

from agents_matrix_connector.api.agent import ADMIN_PREFIX, create_agent_app
from agents_matrix_connector.attachment_fetcher import AttachmentFetcher
from agents_matrix_connector.db import attachment_requests as repo
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.settings import BotSettings

CANARY = "CANARY-HISTORY-5c1e"
NAME = f"{CANARY}.pdf"
TOKEN = "syt_canary_token"
BOT = "@agents:x"
OPEN = "!open:hs"
ANON = "!anon:hs"
FOREIGN = "!foreign:hs"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
PDF = b"%PDF-1.4\n" + b"0" * 100

agent_for = rig.agent_for
agent = rig.agent


@pytest.fixture
async def admin(pool, orchestrator):
    token, digest = make_token()
    app = create_agent_app(pool, orchestrator, make_settings(agent_token_sha256=digest))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agents" + ADMIN_PREFIX
    ) as client:
        yield client, bearer(token)


async def media_row(conn, hs: FakeHomeserver, room: str, media_id: str) -> int:
    url = hs.put_media("x", media_id, PDF)
    m = await add_m(
        conn,
        room,
        NOW,
        sender=rig.ALICE,
        body=f"caption {CANARY}",
        media=("file", "application/pdf", len(PDF), NAME),
    )
    await conn.execute("UPDATE inbound SET media_url = %s WHERE id = %s", (url, m))
    return m


async def bind_room(conn, fake: FakeOrchestrator, room: str, *, anon: bool) -> uuid.UUID:
    session = uuid.uuid4()
    fake.sessions[str(session)] = {"id": str(session), "desired": "running"}
    await rig.add_room(conn, room)
    await rig.allow_room(conn, room)
    await rig.bind(conn, room, session, slug=f"hive-{room[1:4]}")
    if anon:
        await conn.execute(
            "INSERT INTO room_settings (room_id, anonymize_names) VALUES (%s, true)", (room,)
        )
    return session


async def table_text(conn, table: str) -> str:
    cur = await conn.execute(f"SELECT t::text FROM {table} t")  # noqa: S608 - fixed names
    return "\n".join(r[0] for r in await cur.fetchall())


async def test_canary_never_leaks(agent, admin, conn, pool, orchestrator, fake, caplog):
    hs = FakeHomeserver(BOT, TOKEN)
    open_s = await bind_room(conn, fake, OPEN, anon=False)
    anon_s = await bind_room(conn, fake, ANON, anon=True)
    m_open = await media_row(conn, hs, OPEN, "open")
    m_anon = await media_row(conn, hs, ANON, "anon")
    foreign = await media_row(conn, hs, FOREIGN, "foreign")
    client, admin_headers = admin
    failures: list[httpx.Response] = []
    histories: dict[str, list[httpx.Response]] = {OPEN: [], ANON: []}

    def note(r: httpx.Response, room: str | None = None) -> httpx.Response:
        (histories[room] if room and r.status_code == 200 else failures).append(r)
        return r

    with caplog.at_level(logging.DEBUG):
        for room, session in ((OPEN, open_s), (ANON, anon_s)):
            h = rig.as_(session, slug=f"hive-{room[1:4]}")
            note(await agent.get("/history", headers=h), room)
            note(await agent.get("/history", params={"before": "m999999"}, headers=h))
            note(await agent.get("/history", params={"after": f"m{foreign}"}, headers=h))
            note(await agent.get("/history", params={"from": CANARY}, headers=h), room)
            note(await client.get(f"/rooms/{room}/messages", headers=admin_headers), room)
            note(
                await client.get(
                    f"/rooms/{room}/messages", params={"from": CANARY}, headers=admin_headers
                ),
                room,
            )
            note(await agent.post("/attachments", json={"id": f"m{foreign}"}, headers=h))
            note(await agent.post("/attachments", json={"id": CANARY}, headers=h))
            note(await agent.get(f"/attachments/{CANARY}", headers=h))
        ok_open = note(
            await agent.post(
                "/attachments", json={"id": f"m{m_open}"}, headers=rig.as_(open_s, slug="hive-ope")
            )
        )
        ok_anon = note(
            await agent.post(
                "/attachments", json={"id": f"m{m_anon}"}, headers=rig.as_(anon_s, slug="hive-ano")
            )
        )
        assert (ok_open.status_code, ok_anon.status_code) == (202, 202)
        failures.clear()
        for room, session in ((OPEN, open_s), (ANON, anon_s)):
            h = rig.as_(session, slug=f"hive-{room[1:4]}")
            note(await agent.post("/attachments", json={"id": f"m{foreign}"}, headers=h))
            note(await agent.post("/attachments", json={"id": CANARY}, headers=h))
            note(await agent.get(f"/attachments/{CANARY}", headers=h))
            note(await agent.get("/history", params={"before": "m999999"}, headers=h))
            note(await agent.get("/history", params={"after": f"m{foreign}"}, headers=h))
        async with MatrixClient(
            "http://hs", TOKEN, transport=httpx.ASGITransport(app=hs.app)
        ) as matrix:
            settings = BotSettings(
                database_url="postgresql://unused/x",
                orchestrator_url="http://orch",
                orchestrator_token="orch-token",
                homeserver_url="http://hs",
                user_id=BOT,
                access_token=TOKEN,
            )
            fetcher = AttachmentFetcher(orchestrator, matrix, pool, settings)
            await fetcher.tick()
            await fetcher.tick()

    states = [(await repo.get(conn, i)).status for i in (1, 2)]
    assert states == ["sent", "sent"]
    # The test client's own request log (httpx) echoes the query string it sent; the service's
    # lines are the ones that matter.
    service = [r.getMessage() for r in caplog.records if r.name != "httpx"]
    assert service and not any(CANARY in line for line in service)
    assert failures and all(r.status_code >= 400 for r in failures)
    for r in failures:
        assert CANARY not in r.text, r.text
    markers = (CANARY, base64.b64encode(CANARY.encode()).decode())
    for table in ("attachment_requests", "history_crawls"):
        text = await table_text(conn, table)
        assert not any(m in text for m in markers), table

    # The marker is the message text of the open room: allowed there, and only there.
    assert any(CANARY in r.text for r in histories[OPEN])
    for r in histories[ANON]:
        assert NAME not in r.text
        for message in r.json()["messages"]:
            assert message["from"].startswith("user-") or message["from"].startswith("admin-")
            assert message["attachment"]["name"] is None
    assert json.dumps(histories[ANON][0].json()).count(rig.ALICE) == 0
