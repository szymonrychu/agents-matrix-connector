import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agents_common.orchestrator import OrchestratorClient

from agents_matrix_connector.db import inbound, runs
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.prompt import PREAMBLE
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.worker import Worker

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TOKEN = "syt_worker_test_token"
BOT = "@agents:hs"
ALICE = "@facebook_111:hs"
DM = "!dm:hs"
OTHER = "!other:hs"
THIRD = "!third:hs"


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class SpyMatrix:
    """Records the bot's typing calls and forwards them to the real client."""

    def __init__(self, client: MatrixClient):
        self._client = client
        self.typings: list[tuple[str, str, bool, int]] = []
        self.receipts: list[tuple[str, str]] = []

    async def typing(
        self, user_id: str, room_id: str, typing: bool, timeout_ms: int = 30000
    ) -> None:
        self.typings.append((user_id, room_id, typing, timeout_ms))
        await self._client.typing(user_id, room_id, typing, timeout_ms)

    async def send_receipt(self, room_id: str, event_id: str) -> None:
        self.receipts.append((room_id, event_id))
        await self._client.send_receipt(room_id, event_id)


class DiscardAfterCheck:
    """The orchestrator client, but a session is discarded right after it was checked: the
    session answers running and is gone by the time the batch is posted."""

    def __init__(self, client: OrchestratorClient, fake):
        self._client = client
        self._fake = fake

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def get_session(self, session_id):
        session = await self._client.get_session(session_id)
        self._fake.discard_session(str(session_id))
        return session


def make_settings(**overrides) -> BotSettings:
    values = dict(
        database_url="postgresql://unused/x",
        orchestrator_url="http://orch",
        orchestrator_token="orch-token",
        homeserver_url="http://hs",
        user_id=BOT,
        access_token=TOKEN,
        max_new_sessions=3,
    )
    values.update(overrides)
    return BotSettings(**values)


@pytest.fixture
def hs() -> FakeHomeserver:
    fake = FakeHomeserver(BOT, TOKEN)
    for room in (DM, OTHER, THIRD):
        fake.add_room(room, [], {ALICE: "Alice", BOT: "Agents"})
    return fake


@pytest.fixture
async def matrix(hs):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=hs.app)) as c:
        yield SpyMatrix(c)


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def make_worker(orchestrator, matrix, pool, clock):
    def build(client=None, **overrides) -> Worker:
        return Worker(client or orchestrator, matrix, pool, make_settings(**overrides), now=clock)

    return build


@pytest.fixture
def preset(fake, conn):
    """A preset of the fake orchestrator stored as the default preset."""

    async def build(default: bool = True):
        ref = fake.add_preset(f"p-{uuid.uuid4().hex[:6]}")
        if default:
            await conn.execute("UPDATE settings SET default_preset_id = %s WHERE id = 1", (ref.id,))
        return ref

    return build


async def add_room(conn, room_id: str, name: str = "Alice", kind: str = "dm") -> None:
    await conn.execute(
        "INSERT INTO rooms (room_id, kind, name, membership, member_count)"
        " VALUES (%s, %s, %s, 'join', 2)",
        (room_id, kind, name),
    )


async def queue(conn, room_id: str, *bodies: str) -> list[str]:
    event_ids = []
    for body in bodies:
        event_ids.append(f"${uuid.uuid4().hex}")
        await inbound.insert(
            conn,
            event_id=event_ids[-1],
            room_id=room_id,
            sender=ALICE,
            sender_name="Alice A",
            body=body,
            mentioned=False,
            origin_ts=T0,
            status="new",
            reason=None,
        )
    return event_ids


async def queue_at(conn, room_id: str, body: str, origin_ts: datetime | None) -> str:
    event_id = f"${uuid.uuid4().hex}"
    await inbound.insert(
        conn,
        event_id=event_id,
        room_id=room_id,
        sender=ALICE,
        sender_name="Alice A",
        body=body,
        mentioned=False,
        origin_ts=origin_ts,
        status="new",
        reason=None,
    )
    return event_id


async def fetch(conn, query: str, *args) -> list[tuple]:
    cur = await conn.execute(query, args)
    return await cur.fetchall()


async def room_runs(conn, room_id: str) -> list[tuple]:
    return await fetch(
        conn,
        "SELECT id, kind, slug, session_id, message_id, status, reason, preset_id FROM runs"
        " WHERE room_id = %s ORDER BY id",
        room_id,
    )


async def room_rows(conn, room_id: str) -> list[tuple]:
    return await fetch(
        conn,
        "SELECT body, status, reason, run_id FROM inbound WHERE room_id = %s ORDER BY id",
        room_id,
    )


async def finish_handled(conn, run_id: int) -> None:
    """What the tracker does when the run's turn completes."""
    async with conn.transaction():
        assert await runs.finish(conn, run_id, "handled")
        await inbound.finish_run_rows(conn, run_id, "handled")


def session_messages(fake, session_id) -> list[dict]:
    return [m for m in fake.messages.values() if m["session_id"] == str(session_id)]


def marker(run_id: int) -> str:
    return f"<!-- agents-matrix:{run_id} -->"


async def test_first_dispatch_batches_room(conn, fake, preset, make_worker):
    default = await preset()
    room_preset = await preset(default=False)
    await add_room(conn, DM)
    await add_room(conn, OTHER, name="Other")
    await conn.execute(
        "INSERT INTO room_settings (room_id, preset_id) VALUES (%s, %s)", (OTHER, room_preset.id)
    )
    await queue(conn, DM, "hello one", "hello <two>")
    await queue(conn, OTHER, "hi")

    await make_worker().tick()

    [(run_id, kind, slug, session_id, message_id, status, _, preset_id)] = await room_runs(conn, DM)
    assert (kind, slug, status) == ("first", f"mx-{run_id}", "in-progress")
    assert preset_id == default.id
    session = fake.sessions[str(session_id)]
    assert session["slug"] == f"mx-{run_id}"
    [message] = session_messages(fake, session_id)
    assert str(message["id"]) == message_id
    text = message["text"]
    assert text.startswith(PREAMBLE)
    assert '<chat room="Alice" kind="dm">' in text
    assert text.index("hello one") < text.index("hello &lt;two&gt;")
    assert 'facebook_id="111"' in text
    assert text.endswith(marker(run_id))
    assert await room_rows(conn, DM) == [
        ("hello one", "in-progress", None, run_id),
        ("hello <two>", "in-progress", None, run_id),
    ]
    # The room's own preset wins over the default preset.
    [other] = await room_runs(conn, OTHER)
    assert other[7] == room_preset.id
    assert fake.sessions[str(other[3])]["preset_id"] == str(room_preset.id)


async def test_reuse_running_session_bypasses_cap(conn, fake, preset, make_worker):
    await preset()
    for room in (DM, OTHER):
        await add_room(conn, room)
    worker = make_worker(max_new_sessions=1)
    await queue(conn, DM, "one", "two")
    await worker.tick()
    [(first_id, _, _, session_id, *_)] = await room_runs(conn, DM)
    await finish_handled(conn, first_id)
    # Another room takes the only new-session slot.
    await queue(conn, OTHER, "other")
    await worker.tick()
    assert [r[1] for r in await room_runs(conn, OTHER)] == ["first"]

    await queue(conn, DM, "three")
    await worker.tick()

    first, follow = await room_runs(conn, DM)
    follow_id, kind, slug, follow_session, message_id, status, *_ = follow
    assert (kind, status, follow_session) == ("follow", "in-progress", session_id)
    assert len(fake.sessions) == 2
    messages = session_messages(fake, session_id)
    assert len(messages) == 2
    text = messages[1]["text"]
    assert str(messages[1]["id"]) == message_id
    assert text.startswith("New messages in the chat:")
    assert PREAMBLE not in text
    assert "three" in text and "one" not in text
    assert text.endswith(marker(follow_id))
    assert (await room_rows(conn, DM))[2] == ("three", "in-progress", None, follow_id)


async def test_session_gone_falls_back(conn, fake, orchestrator, preset, make_worker):
    await preset()
    await add_room(conn, DM)
    worker = make_worker()
    await queue(conn, DM, "one")
    await worker.tick()
    [(first_id, _, _, old_session, *_)] = await room_runs(conn, DM)
    await finish_handled(conn, first_id)
    await queue(conn, DM, "two", "three")

    await make_worker(DiscardAfterCheck(orchestrator, fake)).tick()

    _, (follow_id, kind, _, session_id, message_id, status, reason, _) = await room_runs(conn, DM)
    assert (kind, session_id, message_id, status, reason) == (
        "follow",
        old_session,
        None,
        "failed",
        "session_gone",
    )
    assert (await room_rows(conn, DM))[1:] == [
        ("two", "new", None, None),
        ("three", "new", None, None),
    ]

    await worker.tick()

    *_, (new_id, kind, slug, new_session, _, status, _, _) = await room_runs(conn, DM)
    assert (kind, slug, status) == ("first", f"mx-{new_id}", "in-progress")
    assert new_session != old_session
    assert fake.sessions[str(new_session)]["slug"] == f"mx-{new_id}"
    [message] = session_messages(fake, new_session)
    assert message["text"].startswith(PREAMBLE)
    assert message["text"].index("two") < message["text"].index("three")
    assert (await room_rows(conn, DM))[1:] == [
        ("two", "in-progress", None, new_id),
        ("three", "in-progress", None, new_id),
    ]
    # The rows went back to new once: one failed follow, one new first run.
    assert [r[1] for r in await room_runs(conn, DM)] == ["first", "follow", "first"]
    # The worker never deletes a session.
    assert all(r.method in ("GET", "POST") for r in fake.requests)


async def test_new_session_cap(conn, fake, preset, make_worker):
    await preset()
    for room in (DM, OTHER):
        await add_room(conn, room)
    worker = make_worker(max_new_sessions=1)
    await queue(conn, DM, "first room")
    await queue(conn, OTHER, "second room", "again")

    await worker.tick()
    await worker.tick()

    [(dm_run, *_)] = await room_runs(conn, DM)
    assert await room_runs(conn, OTHER) == []
    assert await room_rows(conn, OTHER) == [
        ("second room", "new", None, None),
        ("again", "new", None, None),
    ]
    assert len(fake.sessions) == 1

    await finish_handled(conn, dm_run)
    await worker.tick()

    [(other_run, kind, *_)] = await room_runs(conn, OTHER)
    assert kind == "first"
    assert [r[1:] for r in await room_rows(conn, OTHER)] == [
        ("in-progress", None, other_run),
        ("in-progress", None, other_run),
    ]


async def test_no_and_unknown_preset(conn, fake, make_worker):
    unknown = uuid.uuid4()
    for room in (DM, OTHER):
        await add_room(conn, room)
    await conn.execute(
        "INSERT INTO room_settings (room_id, preset_id) VALUES (%s, %s)", (OTHER, unknown)
    )
    await queue(conn, DM, "no preset")
    await queue(conn, OTHER, "unknown one", "unknown two")

    await make_worker().tick()

    assert await room_runs(conn, DM) == []
    assert await room_rows(conn, DM) == [("no preset", "ignored", "no_preset", None)]
    [(run_id, kind, _, session_id, message_id, status, reason, preset_id)] = await room_runs(
        conn, OTHER
    )
    assert (kind, session_id, message_id, status, reason, preset_id) == (
        "first",
        None,
        None,
        "failed",
        "preset_not_found",
        unknown,
    )
    assert await room_rows(conn, OTHER) == [
        ("unknown one", "failed", "preset_not_found", run_id),
        ("unknown two", "failed", "preset_not_found", run_id),
    ]
    assert fake.sessions == {}


async def test_one_run_per_room_and_restart_idempotent(conn, fake, preset, make_worker):
    await preset()
    await add_room(conn, DM)
    await queue(conn, DM, "one")
    await make_worker().tick()
    [(run_id, _, _, session_id, message_id, *_)] = await room_runs(conn, DM)

    # A message arriving while the run is in progress waits for it.
    await queue(conn, DM, "two")
    await make_worker().tick()
    assert len(await room_runs(conn, DM)) == 1
    assert (await room_rows(conn, DM))[1] == ("two", "new", None, None)

    # A crash after the session and the prompt were created, before either id was stored:
    # the restarted worker adopts both instead of creating or posting again.
    await conn.execute(
        "UPDATE runs SET session_id = NULL, message_id = NULL WHERE id = %s", (run_id,)
    )
    await make_worker().tick()

    [(_, _, _, adopted_session, adopted_message, status, *_)] = await room_runs(conn, DM)
    assert (adopted_session, adopted_message, status) == (session_id, message_id, "in-progress")
    assert len(fake.sessions) == 1
    assert len(fake.messages) == 1
    assert (await room_rows(conn, DM))[1] == ("two", "new", None, None)


async def test_typing_on_after_dispatch(conn, hs, matrix, preset, make_worker):
    await preset()
    for room in (DM, OTHER):
        await add_room(conn, room)
    await queue(conn, DM, "one")
    worker = make_worker()

    await worker.tick()

    assert hs.typing_state(DM) is True
    assert matrix.typings == [(BOT, DM, True, 30000)]

    # A Matrix error on typing is logged and ignored: the run stays dispatched.
    hs.fail_next("typing", 403, "M_FORBIDDEN")
    await queue(conn, OTHER, "two")
    await worker.tick()

    [(_, _, _, _, message_id, status, *_)] = await room_runs(conn, OTHER)
    assert message_id is not None and status == "in-progress"
    assert hs.typing_state(OTHER) is False


async def test_handoff_sends_read_receipt(conn, hs, matrix, preset, make_worker):
    await preset()
    await add_room(conn, DM)
    worker = make_worker()
    # The newest event is not the last one inserted; a NULL timestamp sorts lowest.
    await queue_at(conn, DM, "a", T0 + timedelta(seconds=5))
    newest = await queue_at(conn, DM, "b", T0 + timedelta(seconds=9))
    await queue_at(conn, DM, "c", T0 + timedelta(seconds=1))
    await queue_at(conn, DM, "d", None)

    await worker.tick()

    assert matrix.receipts == [(DM, newest)]
    assert hs.receipts(DM) == [newest]
    assert matrix.typings == [(BOT, DM, True, 30000)]

    # A delivery into the running session marks the newest event of its batch.
    [(run_id, *_)] = await room_runs(conn, DM)
    await finish_handled(conn, run_id)
    await queue_at(conn, DM, "e", T0 + timedelta(seconds=20))
    tie_a = await queue_at(conn, DM, "f", None)
    tie_b = await queue_at(conn, DM, "g", None)
    assert tie_a != tie_b
    await worker.tick()

    follow_newest = (await fetch(conn, "SELECT event_id FROM inbound WHERE body = 'e'"))[0][0]
    assert hs.receipts(DM) == [newest, follow_newest]
    assert matrix.receipts == [(DM, newest), (DM, follow_newest)]
    assert matrix.typings == [(BOT, DM, True, 30000)] * 2

    # Equal timestamps, and all-NULL timestamps: the greatest row id wins.
    for ts in (T0 + timedelta(seconds=30), None):
        runs_now = await room_runs(conn, DM)
        await finish_handled(conn, runs_now[-1][0])
        await queue_at(conn, DM, "x", ts)
        last = await queue_at(conn, DM, "y", ts)
        await worker.tick()
        assert hs.receipts(DM)[-1] == last


async def test_receipt_failure_does_not_fail_run(conn, hs, matrix, preset, make_worker, caplog):
    caplog.set_level(logging.WARNING, logger="agents_matrix_connector.worker")
    await preset()
    await add_room(conn, DM)
    await queue(conn, DM, "one")
    hs.fail_next("receipt", 403, "M_FORBIDDEN")

    await make_worker().tick()

    [(_, _, _, _, message_id, status, *_)] = await room_runs(conn, DM)
    assert message_id is not None and status == "in-progress"
    assert hs.receipts(DM) == []
    assert matrix.typings == [(BOT, DM, True, 30000)]
    assert hs.typing_state(DM) is True
    warnings = [r.getMessage() for r in caplog.records if "receipt" in r.getMessage()]
    assert len(warnings) == 1 and DM in warnings[0] and "403" in warnings[0]
    assert TOKEN not in warnings[0]


async def test_no_receipt_without_handoff(conn, hs, matrix, fake, make_worker):
    for room in (DM, OTHER):
        await add_room(conn, room)
    unknown = uuid.uuid4()
    await conn.execute(
        "INSERT INTO room_settings (room_id, preset_id) VALUES (%s, %s)", (OTHER, unknown)
    )
    await queue(conn, DM, "no preset")
    await queue(conn, OTHER, "refused")

    await make_worker().tick()

    assert [r[1] for r in await room_rows(conn, DM)] == ["ignored"]
    assert [r[1] for r in await room_rows(conn, OTHER)] == ["failed"]
    assert matrix.receipts == []
    assert hs.receipts(DM) == [] and hs.receipts(OTHER) == []


async def test_orchestrator_unavailable_retries_with_stored_ids(
    conn, fake, preset, make_worker, clock, caplog
):
    caplog.set_level(logging.WARNING, logger="agents_matrix_connector.worker")
    await preset()
    for room in (DM, OTHER):
        await add_room(conn, room)
    worker = make_worker()
    await queue(conn, DM, "one")

    # The post fails: the run keeps its stored session and waits for the next tick.
    fake.fail_next("post_message", 503)
    await worker.tick()
    [(run_id, _, _, session_id, message_id, status, *_)] = await room_runs(conn, DM)
    assert (message_id, status) == (None, "in-progress")
    assert session_id is not None
    assert (await room_rows(conn, DM))[0][1:] == ("in-progress", None, run_id)

    # Still unavailable within a minute: retried, logged once.
    fake.fail_next("list_messages", 503)
    clock.at = T0 + timedelta(seconds=30)
    await worker.tick()
    assert sum("unavailable" in r.getMessage() for r in caplog.records) == 1

    clock.at = T0 + timedelta(seconds=90)
    await worker.tick()
    [(_, _, _, same_session, message_id, status, *_)] = await room_runs(conn, DM)
    assert (same_session, status) == (session_id, "in-progress")
    assert message_id is not None
    assert len(fake.sessions) == 1 and len(fake.messages) == 1
    await finish_handled(conn, run_id)

    # The session check fails: nothing is claimed this tick.
    await queue(conn, DM, "two")
    fake.fail_next("get_session", 503)
    await worker.tick()
    assert len(await room_runs(conn, DM)) == 1
    assert (await room_rows(conn, DM))[1] == ("two", "new", None, None)

    # The delivery fails: the follow run keeps the session and is delivered next tick.
    fake.fail_next("post_message", 503)
    await worker.tick()
    _, (follow_id, kind, _, follow_session, message_id, status, *_) = await room_runs(conn, DM)
    assert (kind, follow_session, message_id, status) == (
        "follow",
        session_id,
        None,
        "in-progress",
    )
    await worker.tick()
    _, (_, _, _, _, message_id, status, *_) = await room_runs(conn, DM)
    assert message_id is not None and status == "in-progress"
    texts = [m["text"] for m in session_messages(fake, session_id)]
    assert len(texts) == 2 and texts[1].endswith(marker(follow_id))
