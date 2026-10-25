import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agents_common.db.pool import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient

from agents_matrix_connector.chunks import split
from agents_matrix_connector.db import inbound, runs
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.tracker import NOTICE, Tracker

PACKAGE = "agents_matrix_connector"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TOKEN = "syt_tracker_test_token"
BOT = "@hermes:hs"
ALICE = "@facebook_1:hs"
ROOM = "!dm:hs"


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class SpyMatrix:
    """Records the bot's send and typing calls and forwards them to the real client."""

    def __init__(self, client: MatrixClient):
        self._client = client
        self.sends: list[tuple[str, str, str]] = []
        self.typings: list[tuple[str, bool, int]] = []

    async def send_text(self, room_id: str, txn_id: str, body: str) -> str:
        self.sends.append((room_id, txn_id, body))
        return await self._client.send_text(room_id, txn_id, body)

    async def typing(
        self, user_id: str, room_id: str, typing: bool, timeout_ms: int = 30000
    ) -> None:
        self.typings.append((room_id, typing, timeout_ms))
        await self._client.typing(user_id, room_id, typing, timeout_ms)


def make_settings(**overrides) -> BotSettings:
    values = dict(
        database_url="postgresql://unused/x",
        orchestrator_url="http://orch",
        orchestrator_token="orch-token",
        homeserver_url="http://hs",
        user_id=BOT,
        access_token=TOKEN,
        reply_chunk_chars=100,
        run_timeout_seconds=600,
        retention_days=30,
    )
    values.update(overrides)
    return BotSettings(**values)


@pytest.fixture
def url(migrated_db):
    return migrated_db(PACKAGE)


@pytest.fixture
async def pool(url):
    p = await open_pool(url)
    yield p
    await p.close()


@pytest.fixture
def hs() -> FakeHomeserver:
    fake = FakeHomeserver(BOT, TOKEN)
    for room in (ROOM, "!b:hs", "!c:hs", "!d:hs"):
        fake.add_room(room, [], {ALICE: "Alice", BOT: "Hermes"})
    return fake


@pytest.fixture
async def matrix(hs):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=hs.app)) as c:
        yield SpyMatrix(c)


@pytest.fixture
def orch() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
async def orch_client(orch):
    async with OrchestratorClient(
        "http://orch", orch.token, transport=httpx.ASGITransport(app=orch.app)
    ) as c:
        yield c


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def tracker(orch_client, matrix, pool, clock):
    return Tracker(orch_client, matrix, pool, make_settings(), now=clock)


async def seed(pool, orch, orch_client, room=ROOM, kind="first", bodies=("hi",)):
    """An in-progress run of the room holding its claimed inbound rows and a queued message.

    Returns (run id, message id, session id)."""
    preset = orch.add_preset(f"p-{uuid.uuid4().hex[:6]}")
    session = await orch_client.create_session(preset.id, f"s-{uuid.uuid4().hex[:6]}")
    message = await orch_client.post_message(session.id, "prompt")
    async with pool.connection() as conn:
        ref = await runs.create_run(conn, room_id=room, preset_id=str(preset.id), kind=kind)
        for n, body in enumerate(bodies):
            await inbound.insert(
                conn,
                event_id=f"$e{ref.id}-{n}",
                room_id=room,
                sender=ALICE,
                sender_name="Alice",
                body=body,
                mentioned=False,
                origin_ts=None,
                status="new",
                reason=None,
            )
        await inbound.claim_room(conn, room, ref.id)
        await conn.execute(
            "UPDATE runs SET session_id = %s, message_id = %s, started_at = %s WHERE id = %s",
            (session.id, str(message.id), T0, ref.id),
        )
    return ref.id, message.id, str(session.id)


async def fetch(pool, query: str, *args) -> list[tuple]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, args)
        return await cur.fetchall()


async def run_state(pool, run_id) -> tuple:
    return (await fetch(pool, "SELECT status, reason FROM runs WHERE id = %s", run_id))[0]


async def row_states(pool, room=ROOM) -> list[tuple]:
    return await fetch(
        pool, "SELECT status, reason, run_id FROM inbound WHERE room_id = %s ORDER BY id", room
    )


async def outbound_rows(pool, room=ROOM) -> list[tuple]:
    return await fetch(
        pool,
        "SELECT kind, body, txn_id, status, reason, event_id IS NOT NULL FROM outbound"
        " WHERE room_id = %s ORDER BY id",
        room,
    )


def bodies(hs, room=ROOM) -> list[str]:
    sent = hs.sent(room)
    assert all(
        content["msgtype"] == "m.text" and set(content) == {"msgtype", "body"} for content in sent
    )
    return [content["body"] for content in sent]


def test_split_boundaries():
    assert split("", 100) == []
    assert split(" \n\n \t\n", 100) == []
    assert split("hello", 100) == ["hello"]
    assert split("\n\n  code\nmore\n \n", 100) == ["  code\nmore"]
    # A paragraph break is preferred over a later line break within the limit.
    assert split("aaaa\nbbbb\n\ncccc\ndddd", 15) == ["aaaa\nbbbb", "cccc\ndddd"]
    # A whitespace-only line counts as a paragraph break.
    assert split("aaaa\n  \nbbbb cccc dd", 15) == ["aaaa", "bbbb cccc dd"]
    # Without a blank line, the last newline within the limit.
    assert split("aaaa\nbbbb\ncccc", 12) == ["aaaa\nbbbb", "cccc"]
    # A chunk of exactly the limit ends at the break that follows it.
    assert split("aaaa\n\nbbbb", 4) == ["aaaa", "bbbb"]
    # No boundary at all: hard cuts at the limit.
    assert split("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
    text = "\n\n".join(
        "\n".join(f"line {p}.{n} " + "w" * (n * 7 % 40) for n in range(12)) for p in range(30)
    )
    chunks = split(text, 300)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 300 for chunk in chunks)
    assert all(chunk == chunk.strip("\n") for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


async def test_completed_turn_replies(tracker, pool, orch, orch_client, hs, matrix):
    run_id, message_id, _ = await seed(pool, orch, orch_client, bodies=("hi", "there"))
    await tracker.tick()
    assert hs.typing_state(ROOM) is True
    assert bodies(hs) == []

    first, second = "A" * 60, "B" * 60
    orch.complete_turn(message_id, f"{first}\n\n{second}\n")
    await tracker.tick()
    assert bodies(hs) == [first, second]
    assert await run_state(pool, run_id) == ("handled", None)
    assert await row_states(pool) == [("handled", None, run_id), ("handled", None, run_id)]
    assert await outbound_rows(pool) == [
        ("reply", first, f"mxc-{run_id}-1", "sent", None, True),
        ("reply", second, f"mxc-{run_id}-2", "sent", None, True),
    ]
    assert hs.typing_state(ROOM) is False
    assert matrix.typings[-1] == (ROOM, False, 30000)

    other_run, other_message, _ = await seed(pool, orch, orch_client, room="!b:hs")
    orch.complete_turn(other_message, "Hello")
    await tracker.tick()
    await tracker.tick()
    assert bodies(hs, "!b:hs") == ["Hello"]
    assert bodies(hs) == [first, second]
    assert await run_state(pool, other_run) == ("handled", None)

    empty_run, empty_message, _ = await seed(pool, orch, orch_client, room="!c:hs")
    await tracker.tick()
    assert hs.typing_state("!c:hs") is True
    orch.complete_turn(empty_message, " \n\n ")
    await tracker.tick()
    assert await run_state(pool, empty_run) == ("handled", "empty_reply")
    assert await row_states(pool, "!c:hs") == [("handled", "empty_reply", empty_run)]
    assert await outbound_rows(pool, "!c:hs") == []
    assert bodies(hs, "!c:hs") == []
    assert hs.typing_state("!c:hs") is False


async def test_resend_is_idempotent(
    orch_client, pool, orch, hs, matrix, clock, tracker, monkeypatch
):
    run_id, message_id, _ = await seed(pool, orch, orch_client)
    orch.complete_turn(message_id, "Hello")

    async def crash(conn, outbound_id, event_id):
        raise RuntimeError("crash before mark_sent")

    real_mark_sent = runs.mark_sent
    monkeypatch.setattr(runs, "mark_sent", crash)
    with pytest.raises(RuntimeError, match="crash before mark_sent"):
        await tracker.tick()
    assert bodies(hs) == ["Hello"]
    assert await outbound_rows(pool) == [
        ("reply", "Hello", f"mxc-{run_id}-1", "pending", None, False)
    ]

    monkeypatch.setattr(runs, "mark_sent", real_mark_sent)
    restarted = Tracker(orch_client, matrix, pool, make_settings(), now=clock)
    await restarted.tick()
    assert [txn for _, txn, _ in matrix.sends] == [f"mxc-{run_id}-1", f"mxc-{run_id}-1"]
    assert bodies(hs) == ["Hello"]
    assert await outbound_rows(pool) == [("reply", "Hello", f"mxc-{run_id}-1", "sent", None, True)]
    await restarted.tick()
    assert bodies(hs) == ["Hello"]
    assert len(matrix.sends) == 2


async def test_failure_and_timeout_notice(tracker, pool, orch, orch_client, hs, clock):
    message_failed, failed_message, _ = await seed(pool, orch, orch_client, room=ROOM)
    turn_failed, turn_message, _ = await seed(pool, orch, orch_client, room="!b:hs")
    cancelled, _, cancelled_session = await seed(pool, orch, orch_client, room="!c:hs")
    timed_out, _, _ = await seed(pool, orch, orch_client, room="!d:hs")
    await tracker.tick()
    assert all(hs.typing_state(room) for room in (ROOM, "!b:hs", "!c:hs", "!d:hs"))

    orch.fail_turn(failed_message)
    orch.mark_sent(turn_message)
    orch.messages[turn_message]["turn"] = {"status": "failed", "final_text": None}
    orch.discard_session(cancelled_session)
    clock.at = T0 + timedelta(seconds=601)
    await tracker.tick()
    await tracker.tick()

    for run_id, room, reason in (
        (message_failed, ROOM, "message_failed"),
        (turn_failed, "!b:hs", "turn_failed"),
        (cancelled, "!c:hs", "message_cancelled"),
        (timed_out, "!d:hs", "timeout"),
    ):
        assert await run_state(pool, run_id) == ("failed", reason)
        assert await row_states(pool, room) == [("failed", reason, run_id)]
        assert await outbound_rows(pool, room) == [
            ("notice", NOTICE, f"mxc-{run_id}-notice", "sent", None, True)
        ]
        assert bodies(hs, room) == ["Sorry, the agent could not answer that message."]
        assert hs.typing_state(room) is False


async def test_first_run_message_not_found_fails_with_notice(tracker, pool, orch, orch_client, hs):
    run_id, _, session_id = await seed(pool, orch, orch_client)
    orch.remove_session(session_id)
    await tracker.tick()
    assert await run_state(pool, run_id) == ("failed", "message_failed")
    assert await row_states(pool) == [("failed", "message_failed", run_id)]
    assert bodies(hs) == [NOTICE]


async def test_follow_session_gone_requeues(tracker, pool, orch, orch_client, hs, matrix):
    cancelled, _, cancelled_session = await seed(pool, orch, orch_client, kind="follow")
    missing, _, missing_session = await seed(
        pool, orch, orch_client, room="!b:hs", kind="follow", bodies=("one", "two")
    )
    failed, failed_message, _ = await seed(pool, orch, orch_client, room="!c:hs", kind="follow")
    await tracker.tick()

    orch.discard_session(cancelled_session)
    orch.remove_session(missing_session)
    orch.fail_turn(failed_message)
    await tracker.tick()
    await tracker.tick()

    for run_id, room, count in ((cancelled, ROOM, 1), (missing, "!b:hs", 2), (failed, "!c:hs", 1)):
        assert await run_state(pool, run_id) == ("failed", "session_gone")
        assert await row_states(pool, room) == [("new", None, None)] * count
        assert await outbound_rows(pool, room) == []
        assert bodies(hs, room) == []
        assert hs.typing_state(room) is False
    assert matrix.sends == []


async def test_send_errors(tracker, pool, orch, orch_client, hs):
    unavailable, unavailable_message, _ = await seed(pool, orch, orch_client)
    orch.complete_turn(unavailable_message, "Hello")
    hs.fail_next("send_text", 503)
    await tracker.tick()
    assert await run_state(pool, unavailable) == ("handled", None)
    assert await outbound_rows(pool) == [
        ("reply", "Hello", f"mxc-{unavailable}-1", "pending", None, False)
    ]
    assert bodies(hs) == []
    await tracker.tick()
    assert await outbound_rows(pool) == [
        ("reply", "Hello", f"mxc-{unavailable}-1", "sent", None, True)
    ]
    assert bodies(hs) == ["Hello"]
    assert hs.typing_state(ROOM) is False

    forbidden, forbidden_message, _ = await seed(pool, orch, orch_client, room="!b:hs")
    await tracker.tick()
    assert hs.typing_state("!b:hs") is True
    orch.complete_turn(forbidden_message, "Hello")
    hs.fail_next("send_text", 403)
    await tracker.tick()
    assert await outbound_rows(pool, "!b:hs") == [
        ("reply", "Hello", f"mxc-{forbidden}-1", "failed", "forbidden", False)
    ]
    assert bodies(hs, "!b:hs") == []
    assert hs.typing_state("!b:hs") is False
    await tracker.tick()
    assert bodies(hs, "!b:hs") == []


async def test_typing_refreshed(tracker, pool, orch, orch_client, hs, matrix):
    run_id, _, _ = await seed(pool, orch, orch_client)
    async with pool.connection() as conn:
        await runs.create_run(conn, room_id="!b:hs", preset_id=str(uuid.uuid4()), kind="first")
    await tracker.tick()
    await tracker.tick()
    assert matrix.typings == [(ROOM, True, 30000), (ROOM, True, 30000)]
    assert hs.typing_state(ROOM) is True

    hs.fail_next("typing", 503)
    await tracker.tick()
    assert len(matrix.typings) == 3
    assert await run_state(pool, run_id) == ("in-progress", None)
    await tracker.tick()
    assert matrix.typings[-1] == (ROOM, True, 30000)
    assert hs.typing_state(ROOM) is True


async def test_daily_prune(tracker, pool, clock, hs):
    old = T0 - timedelta(days=31)
    recent = T0 - timedelta(days=28)

    async def add_rows(tag: str, at: datetime) -> None:
        async with pool.connection() as conn:
            for status in ("handled", "ignored", "failed", "new"):
                await conn.execute(
                    "INSERT INTO inbound (event_id, room_id, sender, status, received_at)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (f"${tag}-{status}", ROOM, ALICE, status, at),
                )
            for status in ("handled", "failed"):
                await conn.execute(
                    "INSERT INTO runs (room_id, preset_id, kind, status, reason, started_at,"
                    " finished_at) VALUES (%s, %s, 'first', %s, %s, %s, %s)",
                    (ROOM, uuid.uuid4(), status, tag, at, at),
                )
            await conn.execute(
                "INSERT INTO runs (room_id, preset_id, kind, status, reason, started_at)"
                " VALUES (%s, %s, 'first', 'in-progress', %s, %s)",
                (ROOM, uuid.uuid4(), tag, at),
            )
            for status in ("sent", "failed", "pending"):
                await conn.execute(
                    "INSERT INTO outbound (room_id, kind, body, txn_id, status, created_at)"
                    " VALUES (%s, 'reply', 'x', %s, %s, %s)",
                    (ROOM, f"{tag}-{status}", status, at),
                )

    async def remaining() -> tuple[list, list, list]:
        return (
            await fetch(pool, "SELECT event_id FROM inbound ORDER BY id"),
            await fetch(pool, "SELECT reason, status FROM runs ORDER BY id"),
            await fetch(pool, "SELECT txn_id FROM outbound ORDER BY id"),
        )

    await add_rows("old", old)
    await add_rows("recent", recent)
    hs.fail_next("send_text", 503)  # pending rows stay pending
    await tracker.tick()
    inbound_left, runs_left, outbound_left = await remaining()
    assert inbound_left == [
        ("$old-new",),
        ("$recent-handled",),
        ("$recent-ignored",),
        ("$recent-failed",),
        ("$recent-new",),
    ]
    assert runs_left == [
        ("old", "in-progress"),
        ("recent", "handled"),
        ("recent", "failed"),
        ("recent", "in-progress"),
    ]
    assert outbound_left == [
        ("old-pending",),
        ("recent-sent",),
        ("recent-failed",),
        ("recent-pending",),
    ]

    await add_rows("later", T0 + timedelta(hours=23) - timedelta(days=31))
    clock.at = T0 + timedelta(hours=23)
    hs.fail_next("send_text", 503)  # pending rows stay pending
    await tracker.tick()
    assert len((await remaining())[0]) == 9
    clock.at = T0 + timedelta(hours=24)
    hs.fail_next("send_text", 503)  # pending rows stay pending
    await tracker.tick()
    inbound_left, runs_left, outbound_left = await remaining()
    assert ("$later-handled",) not in inbound_left and ("$later-new",) in inbound_left
    assert ("later", "handled") not in runs_left and ("later", "in-progress") in runs_left
    assert ("later-sent",) not in outbound_left and ("later-pending",) in outbound_left
    assert len(inbound_left) == 6
