import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
    Session,
)

from agents_matrix_connector.db import bindings, holds, inbound, runs
from agents_matrix_connector.db.bindings import BindingRow
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.prompt import DEFAULT_PREAMBLE
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.worker import Worker, binding_state, check_action

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TOKEN = "syt_worker_test_token"
BOT = "@agents:hs"
ALICE = "@facebook_111:hs"
DM = "!dm:hs"
OTHER = "!other:hs"
THIRD = "!third:hs"
FOURTH = "!fourth:hs"


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
    assert text.startswith(DEFAULT_PREAMBLE)
    assert f'<chat room="Alice" kind="dm" room_id="{DM}">' in text
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
    assert DEFAULT_PREAMBLE not in text
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
    assert message["text"].startswith(DEFAULT_PREAMBLE)
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


# The hard 1:1 room-session binding (design D9).


def add_session(fake, slug: str, preset_id=None) -> str:
    """A running orchestrator session that this connector did not create (a hive leader, a
    webhook or schedule session)."""
    sid = str(uuid.uuid4())
    fake.sessions[sid] = {
        "id": sid,
        "slug": slug,
        "preset_id": None if preset_id is None else str(preset_id),
        "preset_name": None,
        "desired": "active",
        "created_at": T0.isoformat(),
    }
    return sid


async def bind_agent(conn, room_id: str, session_id: str, send_id: int = 1) -> None:
    """What the sender does when an agent initiates in an unbound flagged room."""
    outcome = await bindings.bind_for_send(
        conn,
        room_id=room_id,
        session_id=uuid.UUID(session_id),
        slug=f"slug-{session_id[:4]}",
        send_id=send_id,
    )
    assert outcome == "bound"


async def room_bindings(conn, room_id: str) -> list[tuple]:
    return await fetch(
        conn,
        "SELECT origin, run_id, session_id, slug, released_at IS NOT NULL, release_reason"
        " FROM room_bindings WHERE room_id = %s ORDER BY id",
        room_id,
    )


async def active_binding(conn, room_id: str) -> tuple | None:
    active = [b for b in await room_bindings(conn, room_id) if not b[4]]
    assert len(active) <= 1
    return active[0] if active else None


async def add_run(conn, room_id: str, session_id, kind: str = "first", finished: bool = True):
    ref = await runs.create_run(conn, room_id=room_id, preset_id=str(uuid.uuid4()), kind=kind)
    await conn.execute("UPDATE runs SET session_id = %s WHERE id = %s", (session_id, ref.id))
    if finished:
        assert await runs.finish(conn, ref.id, "handled")
    return ref.id


def session_checks(fake) -> int:
    return sum(
        r.method == "GET"
        and r.url.path.count("/") == 4
        and r.url.path.startswith("/api/v1/sessions/")
        for r in fake.requests
    )


def _binding(session_id=None) -> BindingRow:
    return BindingRow(
        id=1,
        room_id=DM,
        session_id=session_id,
        slug=None,
        origin="inbound",
        run_id=1,
        send_id=None,
        created_at=T0,
        released_at=None,
        release_reason=None,
    )


def test_routing_decision():
    """The pure decision of `Worker._room` and the binding sweep (no database)."""
    assert binding_state(None) == "unbound"
    assert binding_state(_binding()) == "reserved"
    assert binding_state(_binding(uuid.uuid4())) == "bound"

    running = Session(id=uuid.uuid4(), slug="hive-lead", desired="active")
    assert check_action(running) == "follow"
    # running_session answers None for not found and for desired `deleting`.
    assert check_action(None) == "release"
    assert check_action(NotFound(404, "not_found", "gone")) == "release"
    assert check_action(OrchestratorError(403, "forbidden", "no")) == "keep"
    assert check_action(OrchestratorError(409, "conflict", "busy")) == "keep"
    assert check_action(OrchestratorUnavailable("down")) == "retry"


async def test_first_run_binds_room(conn, fake, preset, make_worker):
    await preset()
    await add_room(conn, DM)
    await queue(conn, DM, "one")
    worker = make_worker()

    # The session cannot be created yet: the claim already reserved the room for the run.
    fake.fail_next("create_session", 503)
    await worker.tick()
    [(run_id, kind, _, session_id, *_)] = await room_runs(conn, DM)
    assert (kind, session_id) == ("first", None)
    assert await room_bindings(conn, DM) == [("inbound", run_id, None, None, False, None)]

    await worker.tick()
    [(_, _, _, session_id, message_id, status, *_)] = await room_runs(conn, DM)
    assert session_id is not None and message_id is not None and status == "in-progress"
    assert await room_bindings(conn, DM) == [
        ("inbound", run_id, session_id, f"mx-{run_id}", False, None)
    ]

    # A retried dispatch with the same ids changes nothing.
    await conn.execute(
        "UPDATE runs SET session_id = NULL, message_id = NULL WHERE id = %s", (run_id,)
    )
    await worker.tick()
    [(_, _, _, adopted, *_)] = await room_runs(conn, DM)
    assert adopted == session_id
    assert await room_bindings(conn, DM) == [
        ("inbound", run_id, session_id, f"mx-{run_id}", False, None)
    ]
    assert len(fake.sessions) == 1


async def test_failed_first_run_releases_reservation(conn, fake, preset, make_worker):
    default = await preset()
    for room in (DM, OTHER):
        await add_room(conn, room)
    await conn.execute(
        "INSERT INTO room_settings (room_id, preset_id) VALUES (%s, %s)", (OTHER, uuid.uuid4())
    )
    await queue(conn, DM, "refused")
    await queue(conn, OTHER, "unknown preset")
    worker = make_worker()

    # DM: the orchestrator refuses the new session; OTHER: its preset does not exist.
    fake.fail_next("create_session", 422)
    await worker.tick()

    [(dm_run, _, _, dm_session, _, dm_status, *_)] = await room_runs(conn, DM)
    assert (dm_session, dm_status) == (None, "failed")
    [(other_run, _, _, other_session, _, other_status, reason, _)] = await room_runs(conn, OTHER)
    assert (other_session, other_status, reason) == (None, "failed", "preset_not_found")
    assert await room_bindings(conn, DM) == [("inbound", dm_run, None, None, True, "run_failed")]
    assert await room_bindings(conn, OTHER) == [
        ("inbound", other_run, None, None, True, "run_failed")
    ]

    await queue(conn, DM, "again")
    await conn.execute(
        "UPDATE room_settings SET preset_id = %s WHERE room_id = %s", (default.id, OTHER)
    )
    await queue(conn, OTHER, "again")
    await worker.tick()

    for room, failed_run in ((DM, dm_run), (OTHER, other_run)):
        _, (new_run, kind, slug, session_id, _, status, *_) = await room_runs(conn, room)
        assert (kind, status) == ("first", "in-progress")
        assert await room_bindings(conn, room) == [
            ("inbound", failed_run, None, None, True, "run_failed"),
            ("inbound", new_run, session_id, slug, False, None),
        ]


async def room_hold(conn, room_id: str) -> tuple | None:
    rows = await fetch(conn, "SELECT until, reason FROM room_holds WHERE room_id = %s", room_id)
    return rows[0] if rows else None


async def test_usage_quota_refusal_holds_room(conn, fake, preset, make_worker, clock, caplog):
    caplog.set_level(logging.INFO, logger="agents_matrix_connector.worker")
    await preset()
    await add_room(conn, DM)
    await queue(conn, DM, "one")
    resets_at = T0 + timedelta(hours=2)
    fake.refuse_usage(resets_at)
    worker = make_worker()

    await worker.tick()

    [(run_id, kind, _, session_id, message_id, status, reason, _)] = await room_runs(conn, DM)
    assert (kind, session_id, message_id) == ("first", None, None)
    assert (status, reason) == ("failed", "usage_quota")
    assert await room_rows(conn, DM) == [("one", "new", None, None)]
    assert await room_bindings(conn, DM) == [("inbound", run_id, None, None, True, "run_failed")]
    until = resets_at + timedelta(seconds=60)
    assert await room_hold(conn, DM) == (until, "usage_quota")
    assert fake.sessions == {}
    assert any("usage_quota" in r.getMessage() and DM in r.getMessage() for r in caplog.records)

    # Messages arriving during the hold stay new, and nothing is asked of the orchestrator.
    posts = sum(r.method == "POST" for r in fake.requests)
    await queue(conn, DM, "two")
    clock.at = until - timedelta(seconds=1)
    await worker.tick()
    assert len(await room_runs(conn, DM)) == 1
    assert [r[:2] for r in await room_rows(conn, DM)] == [("one", "new"), ("two", "new")]
    assert sum(r.method == "POST" for r in fake.requests) == posts

    # At `until` the room is ready again: one new first run carries both messages.
    clock.at = until
    await worker.tick()
    _, (new_id, kind, _, new_session, _, status, *_) = await room_runs(conn, DM)
    assert (kind, status) == ("first", "in-progress") and new_session is not None
    assert [r[:2] for r in await room_rows(conn, DM)] == [
        ("one", "in-progress"),
        ("two", "in-progress"),
    ]
    assert (await active_binding(conn, DM))[1] == new_id


async def test_second_usage_quota_refusal_moves_hold(conn, fake, preset, make_worker, clock):
    await preset()
    await add_room(conn, DM)
    await queue(conn, DM, "one")
    fake.refuse_usage(T0 + timedelta(hours=1), times=2)
    worker = make_worker()

    await worker.tick()
    first_until = T0 + timedelta(hours=1, seconds=60)
    assert await room_hold(conn, DM) == (first_until, "usage_quota")

    clock.at = first_until
    await worker.tick()

    assert [r[5:7] for r in await room_runs(conn, DM)] == [
        ("failed", "usage_quota"),
        ("failed", "usage_quota"),
    ]
    [(until, reason)] = await fetch(conn, "SELECT until, reason FROM room_holds")
    assert until > first_until and reason == "usage_quota"
    assert await fetch(conn, "SELECT count(*) FROM room_holds WHERE room_id = %s", DM) == [(1,)]
    assert (await room_rows(conn, DM))[0][1] == "new"


async def test_hold_does_not_affect_other_rooms(conn, fake, preset, make_worker, clock):
    await preset()
    for room in (DM, OTHER):
        await add_room(conn, room)
    await holds.hold(conn, DM, T0 + timedelta(hours=1), "usage_quota")
    leader = add_session(fake, "hive-lead")
    await bind_agent(conn, OTHER, leader)
    await queue(conn, DM, "held")
    await queue(conn, OTHER, "follow me")

    await make_worker().tick()

    assert await room_runs(conn, DM) == []
    assert await room_rows(conn, DM) == [("held", "new", None, None)]
    [(_, kind, _, session_id, message_id, status, *_)] = await room_runs(conn, OTHER)
    assert (kind, str(session_id), status) == ("follow", leader, "in-progress")
    assert message_id is not None


async def test_unposted_run_refused_for_usage_quota_holds_room(
    conn, fake, preset, make_worker, clock
):
    await preset()
    await add_room(conn, DM)
    await queue(conn, DM, "one")
    worker = make_worker()
    fake.fail_next("create_session", 503)
    await worker.tick()
    [(run_id, *_, status, _, _)] = await room_runs(conn, DM)
    assert status == "in-progress"

    # The retry through `_resume` is refused for the usage quota: the run is released too.
    fake.refuse_usage(T0 + timedelta(hours=1))
    await worker.tick()

    assert [r[5:7] for r in await room_runs(conn, DM)] == [("failed", "usage_quota")]
    assert await room_rows(conn, DM) == [("one", "new", None, None)]
    assert await room_bindings(conn, DM) == [("inbound", run_id, None, None, True, "run_failed")]
    assert (await room_hold(conn, DM))[1] == "usage_quota"


async def test_bound_session_gets_batch_without_preset(conn, fake, make_worker):
    await add_room(conn, DM)
    leader = add_session(fake, "hive-lead")
    await bind_agent(conn, DM, leader)
    await queue(conn, DM, "my answer")

    await make_worker().tick()

    [(run_id, kind, _, session_id, message_id, status, reason, preset_id)] = await room_runs(
        conn, DM
    )
    assert (kind, str(session_id), status, reason, preset_id) == (
        "follow",
        leader,
        "in-progress",
        None,
        None,
    )
    [message] = session_messages(fake, leader)
    assert str(message["id"]) == message_id
    assert "my answer" in message["text"] and message["text"].endswith(marker(run_id))
    assert await room_rows(conn, DM) == [("my answer", "in-progress", None, run_id)]
    assert len(fake.sessions) == 1
    assert await active_binding(conn, DM) == (
        "agent",
        None,
        uuid.UUID(leader),
        f"slug-{leader[:4]}",
        False,
        None,
    )


async def test_batch_goes_only_to_bound_session(conn, fake, preset, make_worker):
    await preset()
    for room in (DM, OTHER):
        await add_room(conn, room)
    bound = add_session(fake, "hive-lead")
    earlier = add_session(fake, "mx-old")
    elsewhere = add_session(fake, "mx-elsewhere")
    # Another running session holds runs in this room, and a newer run of a third session
    # is in another room.
    await add_run(conn, DM, earlier)
    await add_run(conn, DM, earlier, kind="follow")
    await bind_agent(conn, DM, bound, send_id=1)
    await add_run(conn, OTHER, elsewhere)
    await bind_agent(conn, OTHER, elsewhere, send_id=2)
    await queue(conn, DM, "for the bound session")
    worker = make_worker()

    await worker.tick()

    *_, (run_id, kind, _, session_id, _, status, *_) = await room_runs(conn, DM)
    assert (kind, str(session_id), status) == ("follow", bound, "in-progress")
    assert len(session_messages(fake, bound)) == 1
    assert session_messages(fake, earlier) == []
    assert session_messages(fake, elsewhere) == []
    assert len(fake.sessions) == 3

    await finish_handled(conn, run_id)
    await queue(conn, DM, "again")
    await worker.tick()
    assert len(session_messages(fake, bound)) == 2
    assert session_messages(fake, earlier) == [] and session_messages(fake, elsewhere) == []


async def test_gone_bound_session_released(conn, fake, make_worker):
    room_preset = fake.add_preset("room-preset")
    for room in (DM, OTHER):
        await add_room(conn, room)
    await conn.execute(
        "INSERT INTO room_settings (room_id, preset_id) VALUES (%s, %s)", (DM, room_preset.id)
    )
    gone = add_session(fake, "hive-gone")
    deleting = add_session(fake, "hive-deleting")
    await bind_agent(conn, DM, gone, send_id=1)
    await bind_agent(conn, OTHER, deleting, send_id=2)
    fake.remove_session(gone)
    fake.discard_session(deleting)
    await queue(conn, DM, "to a new session")
    await queue(conn, OTHER, "nobody")

    await make_worker().tick()

    # DM has a preset: released, and in the same tick a new mx- session binds it.
    [(run_id, kind, slug, session_id, message_id, status, *_)] = await room_runs(conn, DM)
    assert (kind, slug, status) == ("first", f"mx-{run_id}", "in-progress")
    assert message_id is not None and str(session_id) not in (gone, deleting)
    assert await room_bindings(conn, DM) == [
        ("agent", None, uuid.UUID(gone), f"slug-{gone[:4]}", True, "session_gone"),
        ("inbound", run_id, session_id, f"mx-{run_id}", False, None),
    ]
    assert session_messages(fake, session_id)[0]["text"].startswith(DEFAULT_PREAMBLE)
    # OTHER has no preset: released, the rows ignored and the room stays unbound.
    assert await room_runs(conn, OTHER) == []
    assert await room_rows(conn, OTHER) == [("nobody", "ignored", "no_preset", None)]
    assert await room_bindings(conn, OTHER) == [
        ("agent", None, uuid.UUID(deleting), f"slug-{deleting[:4]}", True, "session_gone"),
    ]


async def test_session_gone_releases_binding(conn, fake, orchestrator, preset, make_worker):
    await preset()
    await add_room(conn, DM)
    worker = make_worker()
    await queue(conn, DM, "one")
    await worker.tick()
    [(first_id, _, _, old_session, *_)] = await room_runs(conn, DM)
    await finish_handled(conn, first_id)
    await queue(conn, DM, "two")

    # The session answers running and is gone when the batch is posted.
    await make_worker(DiscardAfterCheck(orchestrator, fake)).tick()

    _, (_, kind, _, _, _, status, reason, _) = await room_runs(conn, DM)
    assert (kind, status, reason) == ("follow", "failed", "session_gone")
    assert await room_bindings(conn, DM) == [
        ("inbound", first_id, old_session, f"mx-{first_id}", True, "session_gone")
    ]
    assert (await room_rows(conn, DM))[1] == ("two", "new", None, None)

    await worker.tick()

    *_, (new_id, kind, _, new_session, _, status, *_) = await room_runs(conn, DM)
    assert (kind, status) == ("first", "in-progress") and new_session != old_session
    assert await active_binding(conn, DM) == (
        "inbound",
        new_id,
        new_session,
        f"mx-{new_id}",
        False,
        None,
    )


async def test_refused_check_keeps_binding(conn, fake, preset, make_worker, clock, caplog):
    caplog.set_level(logging.WARNING, logger="agents_matrix_connector.worker")
    await preset()
    await add_room(conn, DM)
    leader = add_session(fake, "hive-lead")
    await bind_agent(conn, DM, leader)
    await queue(conn, DM, "answer")
    worker = make_worker()
    binding = await active_binding(conn, DM)

    # A refused check keeps the binding and the rows, logged at most once a minute.
    for at in (T0, T0 + timedelta(seconds=30)):
        clock.at = at
        fake.fail_next("get_session", 403)
        await worker.tick()
        assert await room_runs(conn, DM) == []
        assert await room_rows(conn, DM) == [("answer", "new", None, None)]
        assert await active_binding(conn, DM) == binding
    refused = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert len(refused) == 1 and DM in refused[0]

    # An unavailable orchestrator: retried next tick.
    fake.fail_next("get_session", 503)
    await worker.tick()
    assert await room_runs(conn, DM) == []
    assert await room_rows(conn, DM) == [("answer", "new", None, None)]
    assert await active_binding(conn, DM) == binding
    assert len(fake.sessions) == 1

    await worker.tick()
    [(_, kind, _, session_id, message_id, *_)] = await room_runs(conn, DM)
    assert (kind, str(session_id)) == ("follow", leader) and message_id is not None
    assert len(fake.sessions) == 1
    assert await active_binding(conn, DM) == binding


async def test_binding_sweep_releases_deleted_sessions(conn, fake, make_worker, clock):
    def active(room_id):
        return active_binding(conn, room_id)

    gone, deleting, running = (add_session(fake, f"s-{n}") for n in range(3))
    for n, (room, session) in enumerate(((DM, gone), (OTHER, deleting), (THIRD, running))):
        await bind_agent(conn, room, session, send_id=n + 1)
    assert await bindings.reserve_for_run(conn, FOURTH, 999)
    fake.remove_session(gone)
    fake.discard_session(deleting)
    worker = make_worker()

    # The first sweep is one interval after the worker starts.
    await worker.tick()
    assert all([await active(room) for room in (DM, OTHER, THIRD, FOURTH)])
    assert session_checks(fake) == 0

    clock.at = T0 + timedelta(seconds=60)
    await worker.tick()
    assert (await room_bindings(conn, DM))[0][4:] == (True, "session_gone")
    assert (await room_bindings(conn, OTHER))[0][4:] == (True, "session_gone")
    assert await active(THIRD) is not None
    assert await active(FOURTH) == ("inbound", 999, None, None, False, None)
    assert session_checks(fake) == 3

    # At most once per 60 s.
    fake.remove_session(running)
    clock.at = T0 + timedelta(seconds=90)
    await worker.tick()
    assert await active(THIRD) is not None
    assert session_checks(fake) == 3
    clock.at = T0 + timedelta(seconds=120)
    await worker.tick()
    assert (await room_bindings(conn, THIRD))[0][4:] == (True, "session_gone")
    assert await active(FOURTH) is not None

    # An unavailable orchestrator ends the sweep without releasing anything.
    later = [add_session(fake, f"later-{n}") for n in range(2)]
    for n, (room, session) in enumerate(zip((DM, OTHER), later, strict=True)):
        await bind_agent(conn, room, session, send_id=10 + n)
        fake.remove_session(session)
    checks = session_checks(fake)
    fake.fail_next("get_session", 503)
    clock.at = T0 + timedelta(seconds=180)
    await worker.tick()
    assert session_checks(fake) == checks + 1
    assert await active(DM) is not None and await active(OTHER) is not None
    clock.at = T0 + timedelta(seconds=200)
    await worker.tick()
    assert await active(DM) is not None and await active(OTHER) is not None
    clock.at = T0 + timedelta(seconds=240)
    await worker.tick()
    assert await active(DM) is None and await active(OTHER) is None

    # A refused check keeps that binding; the sweep goes on with the next one.
    refused, gone_too = add_session(fake, "refused"), add_session(fake, "gone-too")
    await bind_agent(conn, DM, refused, send_id=20)
    await bind_agent(conn, OTHER, gone_too, send_id=21)
    fake.remove_session(gone_too)
    fake.fail_next("get_session", 403)
    clock.at = T0 + timedelta(seconds=300)
    await worker.tick()
    assert (await active(DM))[2] == uuid.UUID(refused)
    assert await active(OTHER) is None
    assert await active(FOURTH) is not None


async def test_follow_run_preset_from_session(conn, fake, make_worker):
    session_preset = fake.add_preset("session-preset")
    room_preset = fake.add_preset("room-preset")
    for room in (DM, OTHER, THIRD):
        await add_room(conn, room)
    await conn.execute(
        "INSERT INTO room_settings (room_id, preset_id) VALUES (%s, %s), (%s, %s)",
        (DM, room_preset.id, OTHER, room_preset.id),
    )
    with_preset = add_session(fake, "with-preset", preset_id=session_preset.id)
    without = add_session(fake, "without")
    neither = add_session(fake, "neither")
    for n, (room, session) in enumerate(((DM, with_preset), (OTHER, without), (THIRD, neither))):
        await bind_agent(conn, room, session, send_id=n + 1)
        await queue(conn, room, f"to {room}")
    worker = make_worker()

    await worker.tick()

    # The session's preset; else the room's effective preset; else none, without an error.
    expected = {DM: session_preset.id, OTHER: room_preset.id, THIRD: None}
    for room, preset_id in expected.items():
        [(run_id, kind, _, _, message_id, status, reason, stored)] = await room_runs(conn, room)
        assert (kind, status, reason, stored) == ("follow", "in-progress", None, preset_id)
        assert message_id is not None

    # The default preset is the room's effective preset when the room has none.
    default = fake.add_preset("default")
    await conn.execute("UPDATE settings SET default_preset_id = %s WHERE id = 1", (default.id,))
    [(third_run, *_)] = await room_runs(conn, THIRD)
    await finish_handled(conn, third_run)
    await queue(conn, THIRD, "again")
    await worker.tick()
    _, (_, kind, _, _, _, _, _, stored) = await room_runs(conn, THIRD)
    assert (kind, stored) == ("follow", default.id)


async def test_preamble_for_session_new_to_room(conn, fake, make_worker):
    for room in (DM, OTHER, THIRD):
        await add_room(conn, room)
    leader = add_session(fake, "hive-lead")
    await bind_agent(conn, DM, leader)
    await queue(conn, DM, "first answer")
    worker = make_worker()

    await worker.tick()

    [(run_id, *_)] = await room_runs(conn, DM)
    [message] = session_messages(fake, leader)
    assert message["text"].startswith(DEFAULT_PREAMBLE)
    assert f'<chat room="Alice" kind="dm" room_id="{DM}">' in message["text"]
    assert "first answer" in message["text"]

    # The session has an earlier run in the room now.
    await finish_handled(conn, run_id)
    await queue(conn, DM, "second answer")
    await worker.tick()
    [_, second] = session_messages(fake, leader)
    assert second["text"].startswith("New messages in the chat:")
    assert DEFAULT_PREAMBLE not in second["text"] and "second answer" in second["text"]

    # A session whose earlier runs are in another room is new to this one.
    other = add_session(fake, "hive-other")
    await add_run(conn, OTHER, other, kind="follow")
    await bind_agent(conn, THIRD, other, send_id=2)
    await queue(conn, THIRD, "hello")
    await worker.tick()
    [message] = session_messages(fake, other)
    assert message["text"].startswith(DEFAULT_PREAMBLE)


async def test_resume_keeps_preamble_for_session_new_to_room(conn, fake, make_worker):
    await add_room(conn, DM)
    leader = add_session(fake, "hive-lead")
    await bind_agent(conn, DM, leader)
    worker = make_worker()

    # The first delivery fails; the retry through Worker._resume keeps the DEFAULT_PREAMBLE, the run
    # itself not counting as an earlier run of the session in the room.
    await queue(conn, DM, "one")
    fake.fail_next("post_message", 503)
    await worker.tick()
    [(run_id, kind, _, _, message_id, status, *_)] = await room_runs(conn, DM)
    assert (kind, message_id, status) == ("follow", None, "in-progress")
    assert session_messages(fake, leader) == []
    await worker.tick()
    [message] = session_messages(fake, leader)
    assert message["text"].startswith(DEFAULT_PREAMBLE) and message["text"].endswith(marker(run_id))
    [(_, _, _, _, message_id, *_)] = await room_runs(conn, DM)
    assert message_id == str(message["id"])

    # A resumed follow run into a session with an earlier run in the room: no DEFAULT_PREAMBLE.
    await finish_handled(conn, run_id)
    await queue(conn, DM, "two")
    fake.fail_next("post_message", 503)
    await worker.tick()
    _, (follow_id, _, _, _, message_id, *_) = await room_runs(conn, DM)
    assert message_id is None
    await worker.tick()
    [_, resumed] = session_messages(fake, leader)
    assert resumed["text"].startswith("New messages in the chat:")
    assert DEFAULT_PREAMBLE not in resumed["text"] and resumed["text"].endswith(marker(follow_id))


async def _first_text(conn, fake, make_worker, rule_id=None):
    await add_room(conn, DM)
    [event_id] = await queue(conn, DM, "hello")
    await conn.execute("UPDATE inbound SET rule_id = %s WHERE event_id = %s", (rule_id, event_id))
    await make_worker().tick()
    [(_, _, _, session_id, *_)] = await room_runs(conn, DM)
    [message] = session_messages(fake, session_id)
    return message["text"]


async def test_preamble_default_when_nothing_overrides(conn, fake, make_worker, preset):
    await preset()
    text = await _first_text(conn, fake, make_worker)
    assert text.startswith(DEFAULT_PREAMBLE + "\n\n<chat")


async def test_global_preamble_replaces_default_and_renders_variables(
    conn, fake, make_worker, preset
):
    await preset()
    await conn.execute(
        "UPDATE settings SET preamble = %s WHERE id = 1",
        ("Global for {{ room_name }} {{ room_id }} from {{ sender }} at {{ now }}",),
    )
    text = await _first_text(conn, fake, make_worker)
    assert text.startswith(f"Global for Alice {DM} from Alice A at {T0.isoformat()}\n\n<chat")


async def test_rule_preamble_beats_global(conn, fake, make_worker, preset):
    await preset()
    await conn.execute("UPDATE settings SET preamble = 'Global' WHERE id = 1")
    [(rule_id,)] = await (
        await conn.execute(
            "INSERT INTO access_rules (list, kind, value, note, preamble)"
            " VALUES ('allow', 'room', %s, 'family', 'Rule {{ rule_name }}') RETURNING id",
            (DM,),
        )
    ).fetchall()
    text = await _first_text(conn, fake, make_worker, rule_id)
    assert text.startswith("Rule family\n\n<chat")


async def test_rule_without_preamble_falls_to_global(conn, fake, make_worker, preset):
    await preset()
    await conn.execute("UPDATE settings SET preamble = 'Global' WHERE id = 1")
    [(rule_id,)] = await (
        await conn.execute(
            "INSERT INTO access_rules (list, kind, value)"
            " VALUES ('allow', 'room', %s) RETURNING id",
            (DM,),
        )
    ).fetchall()
    text = await _first_text(conn, fake, make_worker, rule_id)
    assert text.startswith("Global\n\n<chat")


async def test_broken_stored_preamble_falls_back_to_default(conn, fake, make_worker, preset):
    await preset()
    await conn.execute("UPDATE settings SET preamble = '{{ nope }}' WHERE id = 1")
    text = await _first_text(conn, fake, make_worker)
    assert text.startswith(DEFAULT_PREAMBLE + "\n\n<chat")
