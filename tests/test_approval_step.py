"""The approval poster against the fake homeserver and the fake taskboard delegate."""

import httpx
import pytest

from agents_matrix_connector.approval_step import ApprovalStep
from agents_matrix_connector.approvals import TaskboardDelegate, approval_text, digest_text
from agents_matrix_connector.db import approvals as repo
from agents_matrix_connector.discovery import ApprovalEvent
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


APPROVER = "@me:x"


async def answer_by_reaction(conn, decision_id: int, key: str = "\N{THUMBS UP SIGN}") -> str:
    """Record approval `decision_id` as posted and the approver's reaction on it as pending."""
    target = f"$a{decision_id}"
    await repo.record_message(conn, decision_id, ROOM, target)
    event_id = f"$r{decision_id}"
    recorded = await repo.record_answer_event(
        conn, ROOM, ApprovalEvent(event_id, APPROVER, target, key, None)
    )
    assert recorded
    return event_id


async def answer_status(conn, event_id: str) -> tuple:
    cur = await conn.execute(
        "SELECT status, results FROM approval_answers WHERE event_id = %s", (event_id,)
    )
    return await cur.fetchone()


async def test_answered_marks_message_answered(step, hs, board, conn):
    board.approvals = {412: item(412)}
    event_id = await answer_by_reaction(conn, 412)
    await step.tick()
    assert board.answers == [
        {"items": [{"id": 412, "verdict": "go", "answer_ref": event_id}], "source": "matrix"}
    ]
    status, results = await answer_status(conn, event_id)
    assert status == "done"
    assert results == [{"id": 412, "result": "answered", "task_key": "T-88"}]
    cur = await conn.execute("SELECT status FROM approval_messages WHERE decision_id = 412")
    assert (await cur.fetchone())[0] == "answered"
    assert hs.sent(ROOM) == []
    await step.tick()
    assert len(board.answers) == 1


async def test_already_answered_gets_short_reply(step, hs, board, conn):
    board.approvals = {412: item(412, "answered")}
    first = await answer_by_reaction(conn, 412, "\N{THUMBS DOWN SIGN}")
    await step.tick()
    assert board.answers[0]["items"] == [{"id": 412, "verdict": "no_go", "answer_ref": first}]
    assert hs.sent_events(ROOM) == [
        (
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": "D-412 already answered.",
                "m.relates_to": {"m.in_reply_to": {"event_id": first}},
            },
        )
    ]
    cur = await conn.execute("SELECT status FROM approval_messages WHERE decision_id = 412")
    assert (await cur.fetchone())[0] == "closed"  # not answered here; no longer open
    assert (await answer_status(conn, first))[0] == "done"


async def test_digest_reply_lists_every_unanswered_result(step, hs, board, conn):
    board.approvals = {412: item(412, "answered"), 413: item(413, "expired"), 414: item(414)}
    for i in (412, 413, 414):
        await repo.record_message(conn, i, ROOM, f"$a{i}")
    await repo.record_digest(conn, ROOM, "$d1", [412, 413, 414, 415])
    recorded = await repo.record_answer_event(
        conn, ROOM, ApprovalEvent("$t", APPROVER, "$d1", None, "all go")
    )
    assert recorded
    await step.tick()
    assert bodies(hs) == ["D-412 already answered. D-413 expired. D-415 is not open."]
    cur = await conn.execute("SELECT decision_id, status FROM approval_messages ORDER BY 1")
    assert await cur.fetchall() == [(412, "closed"), (413, "closed"), (414, "answered")]


async def test_delegate_down_keeps_answer_pending(pool, matrix, hs, settings, conn):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    event_id = await answer_by_reaction(conn, 412)
    d = TaskboardDelegate("http://tb", "tb-token", transport=httpx.MockTransport(refuse))
    await ApprovalStep(pool, matrix, d, settings).tick()
    await d.aclose()
    assert await answer_status(conn, event_id) == ("pending", None)
    assert hs.sent(ROOM) == []


async def test_refused_answer_is_finished_with_the_error(pool, matrix, hs, settings, conn):
    def invalid(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(422, json={})
        return httpx.Response(200, json={"items": []})

    event_id = await answer_by_reaction(conn, 412)
    d = TaskboardDelegate("http://tb", "tb-token", transport=httpx.MockTransport(invalid))
    await ApprovalStep(pool, matrix, d, settings).tick()
    await d.aclose()
    assert await answer_status(conn, event_id) == ("done", [{"error": 422}])
    assert hs.sent_events(ROOM) == [
        (
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": "Could not answer D-412: 422.",
                "m.relates_to": {"m.in_reply_to": {"event_id": event_id}},
            },
        )
    ]


async def test_not_found_replies_and_finishes(pool, matrix, hs, settings, conn):
    def missing(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(404, json={})
            if request.method == "POST"
            else httpx.Response(200, json={"items": []})
        )

    event_id = await answer_by_reaction(conn, 412)
    d = TaskboardDelegate("http://tb", "tb-token", transport=httpx.MockTransport(missing))
    await ApprovalStep(pool, matrix, d, settings).tick()
    await d.aclose()
    assert await answer_status(conn, event_id) == ("done", [{"error": 404}])
    assert bodies(hs) == ["Could not answer D-412: 404."]


@pytest.mark.parametrize("status", [401, 403, 408, 429])
async def test_auth_and_throttle_errors_keep_answer_pending(
    pool, matrix, hs, settings, conn, status
):
    def refuse(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(status, json={})
            if request.method == "POST"
            else httpx.Response(200, json={"items": []})
        )

    event_id = await answer_by_reaction(conn, 412)
    d = TaskboardDelegate("http://tb", "tb-token", transport=httpx.MockTransport(refuse))
    await ApprovalStep(pool, matrix, d, settings).tick()
    await d.aclose()
    assert await answer_status(conn, event_id) == ("pending", None)
    assert hs.sent(ROOM) == []
