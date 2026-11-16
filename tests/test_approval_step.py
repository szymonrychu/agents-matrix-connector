"""The approval poster against the fake homeserver and the fake taskboard delegate."""

import httpx
import pytest

from agents_matrix_connector.approval_step import ApprovalStep
from agents_matrix_connector.approvals import TaskboardDelegate, approval_text, digest_text
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.fake_taskboard import FakeTaskboard
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.settings import BotSettings

BOT = "@agents:x"
TOKEN = "hs-secret-token"
ROOM = "!appr:x"


def item(i: int, status: str = "open") -> dict:
    return {
        "id": i,
        "status": status,
        "asking_slug": "mx-88",
        "task_key": "T-88",
        "question": f"merge PR {i}?",
        "subject": None,
    }


@pytest.fixture
def hs():
    server = FakeHomeserver(BOT, TOKEN)
    server.add_room(ROOM, [], {BOT: "join"})
    return server


@pytest.fixture
def board():
    return FakeTaskboard("tb-token")


@pytest.fixture
async def matrix(hs):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=hs.app)) as c:
        yield c


@pytest.fixture
async def delegate(board):
    d = TaskboardDelegate("http://tb", "tb-token", transport=httpx.ASGITransport(app=board.app))
    yield d
    await d.aclose()


@pytest.fixture
def settings(url) -> BotSettings:
    return BotSettings(
        database_url=url,
        orchestrator_url="http://orch",
        orchestrator_token="t",
        homeserver_url="http://hs",
        user_id=BOT,
        access_token=TOKEN,
        approvals_room=ROOM,
        taskboard_url="http://tb",
        taskboard_token="tb-token",
        approver_ids=["@me:x"],
    )


@pytest.fixture
def step(pool, matrix, delegate, settings):
    return ApprovalStep(pool, matrix, delegate, settings)


def bodies(hs) -> list[str]:
    return [c["body"] for c in hs.sent(ROOM)]


async def test_posts_each_new_approval_once_then_digest(step, hs, board, conn):
    board.approvals = {412: item(412), 413: item(413)}
    await step.tick()
    assert bodies(hs) == [
        approval_text(item(412)),
        approval_text(item(413)),
        digest_text([412, 413]),
    ]
    await step.tick()
    assert len(hs.sent(ROOM)) == 3
    cur = await conn.execute("SELECT status, digest_id FROM approval_messages ORDER BY decision_id")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["posted", "posted"]
    assert rows[0][1] is not None and rows[0][1] == rows[1][1]


async def test_single_new_approval_has_no_digest(step, hs, board, conn):
    board.approvals = {412: item(412)}
    await step.tick()
    assert bodies(hs) == [approval_text(item(412))]
    cur = await conn.execute("SELECT count(*) FROM approval_digests")
    assert (await cur.fetchone())[0] == 0


async def test_gone_approval_is_closed(step, hs, board, conn):
    board.approvals = {412: item(412)}
    await step.tick()
    board.approvals = {}
    await step.tick()
    cur = await conn.execute("SELECT status FROM approval_messages WHERE decision_id = 412")
    assert (await cur.fetchone())[0] == "closed"
    assert len(hs.sent(ROOM)) == 1


async def test_delegate_down_posts_nothing(pool, matrix, hs, settings):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    d = TaskboardDelegate("http://tb", "tb-token", transport=httpx.MockTransport(refuse))
    await ApprovalStep(pool, matrix, d, settings).tick()
    await d.aclose()
    assert hs.sent(ROOM) == []


async def test_step_is_noop_without_delegate(pool, matrix, hs, settings):
    await ApprovalStep(pool, matrix, None, settings).tick()
    assert hs.sent(ROOM) == []
