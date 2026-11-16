"""Inbound approval answers: reactions and quoted replies in the approvals room, read by sync."""

from datetime import UTC, datetime

import httpx
import pytest

from agents_matrix_connector.approval_step import ApprovalStep
from agents_matrix_connector.approvals import TaskboardDelegate
from agents_matrix_connector.db import approvals as repo
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.fake_taskboard import FakeTaskboard
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.sync import SyncLoop

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
TOKEN = "syt_approvals_test_token"
BOT = "@hermes:hs"
SELF_FB = "@facebook_999:hs"
BRIDGE = "@facebookbot:hs"
APPROVER = "@owner:hs"
STRANGER = "@stranger:hs"
APPROVALS = "!approvals:hs"
THUMBS_UP = "\N{THUMBS UP SIGN}"


def make_settings() -> BotSettings:
    return BotSettings(
        database_url="postgresql://unused/x",
        orchestrator_url="http://orch:8000",
        orchestrator_token="orch-token",
        homeserver_url="http://hs",
        user_id=BOT,
        self_identities=[SELF_FB],
        bridge_bots=[BRIDGE],
        access_token=TOKEN,
        sync_timeout_seconds=1,
        approvals_room=APPROVALS,
        taskboard_url="http://tb",
        taskboard_token="tb-token",
        approver_ids=[APPROVER],
    )


@pytest.fixture
def hs() -> FakeHomeserver:
    server = FakeHomeserver(BOT, TOKEN)
    server.add_room(
        APPROVALS,
        [],
        {BOT: "hermes", APPROVER: "Owner", SELF_FB: "Hermes", BRIDGE: "bridge", STRANGER: "S"},
    )
    return server


@pytest.fixture
async def matrix(hs):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=hs.app)) as c:
        yield c


@pytest.fixture
async def loop(hs, matrix, pool, conn):
    """A synced loop with approval 412 ($a412) and 413 in digest $d1, 414 posted outside it."""
    await conn.execute(
        "INSERT INTO access_rules (list, kind, value) VALUES ('allow', 'room', %s)", (APPROVALS,)
    )
    sync = SyncLoop(matrix, pool, make_settings(), now=lambda: T0)
    await sync.run_once()
    for i in (412, 413, 414):
        await repo.record_message(conn, i, APPROVALS, f"$a{i}")
    await repo.record_digest(conn, APPROVALS, "$d1", [412, 413])
    return sync


def react(hs, sender: str, target: str, key: str = THUMBS_UP) -> str:
    relates = {"rel_type": "m.annotation", "event_id": target, "key": key}
    return hs.post(APPROVALS, sender, {"m.relates_to": relates}, event_type="m.reaction")


def reply(hs, sender: str, target: str, body: str) -> str:
    content = {
        "msgtype": "m.text",
        "body": f"> <{BOT}> the approval\n\n{body}",
        "m.relates_to": {"m.in_reply_to": {"event_id": target}},
    }
    return hs.post(APPROVALS, sender, content)


async def answers(conn) -> list[tuple]:
    cur = await conn.execute(
        "SELECT event_id, room_id, sender, target_event_id, verdict, status"
        " FROM approval_answers ORDER BY created_at, event_id"
    )
    return await cur.fetchall()


async def inbound_event_ids(conn) -> list[str]:
    cur = await conn.execute("SELECT event_id FROM inbound ORDER BY id")
    return [row[0] for row in await cur.fetchall()]


async def test_reaction_on_posted_approval_records_go(hs, loop, conn):
    event_id = react(hs, APPROVER, "$a412")
    await loop.run_once()
    assert await answers(conn) == [(event_id, APPROVALS, APPROVER, "$a412", "go", "pending")]
    assert await inbound_event_ids(conn) == []


async def test_reaction_from_bot_or_self_ignored(hs, loop, conn):
    react(hs, BOT, "$a412")
    react(hs, SELF_FB, "$a412")
    react(hs, BRIDGE, "$a412")
    react(hs, STRANGER, "$a412")
    reply(hs, BOT, "$a412", "go")
    await loop.run_once()
    assert await answers(conn) == []


async def test_reaction_on_unknown_event_ignored(hs, loop, conn):
    react(hs, APPROVER, "$other")
    await loop.run_once()
    assert await answers(conn) == []


async def test_ambiguous_reply_passes_to_session(hs, loop, conn):
    event_id = reply(hs, APPROVER, "$a412", "go but later")
    await loop.run_once()
    assert await answers(conn) == []
    assert await inbound_event_ids(conn) == [event_id]


async def test_reply_to_approval_records_no_go(hs, loop, conn):
    event_id = reply(hs, APPROVER, "$a412", "No-go.")
    await loop.run_once()
    assert await answers(conn) == [(event_id, APPROVALS, APPROVER, "$a412", "no_go", "pending")]
    assert await inbound_event_ids(conn) == []


async def test_go_reply_to_digest_is_not_a_digest_answer(hs, loop, conn):
    event_id = reply(hs, APPROVER, "$d1", "go")
    await loop.run_once()
    assert await answers(conn) == []
    assert await inbound_event_ids(conn) == [event_id]


async def test_digest_all_go_answers_exactly_digest_ids(hs, loop, matrix, pool, conn):
    event_id = reply(hs, APPROVER, "$d1", "all go")
    await loop.run_once()
    board = FakeTaskboard("tb-token")
    board.approvals = {
        i: {"id": i, "status": "open", "task_key": "T-88", "question": "q", "subject": None}
        for i in (412, 413, 414)
    }
    delegate = TaskboardDelegate(
        "http://tb", "tb-token", transport=httpx.ASGITransport(app=board.app)
    )
    await ApprovalStep(pool, matrix, delegate, make_settings()).tick()
    await delegate.aclose()
    assert board.answers == [
        {
            "items": [
                {"id": 412, "verdict": "go", "answer_ref": event_id},
                {"id": 413, "verdict": "go", "answer_ref": event_id},
            ],
            "source": "matrix",
        }
    ]
    cur = await conn.execute("SELECT decision_id, status FROM approval_messages ORDER BY 1")
    assert await cur.fetchall() == [(412, "answered"), (413, "answered"), (414, "posted")]


async def test_reaction_in_a_sync_gap_is_backfilled(hs, loop, conn):
    hs.limit_timeline(2)
    event_id = react(hs, APPROVER, "$a412")
    for n in range(3):
        hs.post(APPROVALS, STRANGER, {"msgtype": "m.text", "body": f"filler {n}"})
    await loop.run_once()
    assert [row[0] for row in await answers(conn)] == [event_id]
