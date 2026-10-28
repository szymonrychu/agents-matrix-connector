"""The bot's sender step: agent sends claimed, re-checked, bound, delivered and settled
(design D4, D7, D8, D9).

The api's part is seeded with `sends.insert` (a `queued` row, as the api stores it); the sender
claims it and the tracker delivers its outbound rows to the fake homeserver.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from agents_common.db.pool import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient

from agents_matrix_connector import bot
from agents_matrix_connector.db import inbound, runs, sends
from agents_matrix_connector.db.sends import SendRow
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import MatrixAuthError, MatrixClient, MatrixError
from agents_matrix_connector.sender import Plan, Sender, event_content, outbound_plan
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.tracker import Tracker

PACKAGE = "agents_matrix_connector"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TOKEN = "syt_sender_test_token"
BOT = "@hermes:hs"
ALICE = "@facebook_1:hs"
BOB = "@facebook_2:hs"
CAROL = "@facebook_3:hs"
FACEBOOK_IDS = {ALICE: "1", BOB: "2", CAROL: "3", BOT: None}
ROOMS = [f"!r{n}:hs" for n in range(1, 10)]
R1, R2, R3, R4, R5, R6, R7, R8, R9 = ROOMS


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class SpyMatrix:
    """Records every event the tracker sends; `fail` maps a txn id to the error to raise once."""

    def __init__(self, client: MatrixClient):
        self._client = client
        self.events: list[tuple[str, str, str, dict]] = []
        self.fail: dict[str, MatrixError] = {}

    async def send_event(self, room_id: str, event_type: str, txn_id: str, content: dict) -> str:
        self.events.append((room_id, event_type, txn_id, content))
        if txn_id in self.fail:
            raise self.fail.pop(txn_id)
        return await self._client.send_event(room_id, event_type, txn_id, content)

    async def typing(
        self, user_id: str, room_id: str, typing: bool, timeout_ms: int = 30000
    ) -> None:
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
    for room in ROOMS:
        fake.add_room(room, [], {ALICE: "Alice", BOB: "Bob", BOT: "Hermes"})
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
def sender(matrix, pool, clock):
    return Sender(matrix, pool, make_settings(), now=clock)


@pytest.fixture
def tracker(orch_client, matrix, pool, clock):
    return Tracker(orch_client, matrix, pool, make_settings(), now=clock)


# --- seeding -----------------------------------------------------------------------------------


async def execute(pool, query: str, *args) -> list[tuple]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, args)
        return await cur.fetchall() if cur.description else []


async def add_room(pool, room_id, *, members=(ALICE,), kind="dm", flag=None):
    await execute(
        pool,
        "INSERT INTO rooms (room_id, kind, membership, member_count) VALUES (%s, %s, 'join', %s)",
        room_id,
        kind,
        len(members),
    )
    for user_id in members:
        await execute(
            pool,
            "INSERT INTO users (user_id, facebook_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            user_id,
            FACEBOOK_IDS[user_id],
        )
        await execute(
            pool,
            "INSERT INTO room_members (room_id, user_id, membership) VALUES (%s, %s, 'join')",
            room_id,
            user_id,
        )
    if flag is not None:
        await set_flag(pool, room_id, flag)


async def set_flag(pool, room_id, flag):
    await execute(
        pool,
        "INSERT INTO room_settings (room_id, agents_may_initiate) VALUES (%s, %s)"
        " ON CONFLICT (room_id) DO UPDATE SET agents_may_initiate = EXCLUDED.agents_may_initiate",
        room_id,
        flag,
    )


async def add_rule(pool, list_, kind, value) -> int:
    rows = await execute(
        pool,
        "INSERT INTO access_rules (list, kind, value) VALUES (%s, %s, %s) RETURNING id",
        list_,
        kind,
        value,
    )
    return rows[0][0]


async def allowed_room(pool, room_id, **kwargs):
    await add_room(pool, room_id, **kwargs)
    await add_rule(pool, "allow", "room", room_id)


async def bind(pool, room_id, session_id, *, send_id=999):
    """An active agent binding made by an earlier send (not one of the test's sends)."""
    await execute(
        pool,
        "INSERT INTO room_bindings (room_id, session_id, slug, origin, send_id)"
        " VALUES (%s, %s, 'earlier', 'agent', %s)",
        room_id,
        session_id,
        send_id,
    )


async def reserve(pool, room_id):
    """A first run in progress without a session yet reserves the room."""
    rows = await execute(
        pool,
        "INSERT INTO runs (room_id, preset_id, kind, slug, status)"
        " VALUES (%s, %s, 'first', 'mx-1', 'in-progress') RETURNING id",
        room_id,
        uuid.uuid4(),
    )
    await execute(
        pool,
        "INSERT INTO room_bindings (room_id, origin, run_id) VALUES (%s, 'inbound', %s)",
        room_id,
        rows[0][0],
    )


async def queue(
    pool,
    session_id,
    room_id,
    *,
    kind="message",
    text="hello",
    key=None,
    reply_to=None,
    ref_event_id=None,
    slug="hive-1",
) -> int:
    async with pool.connection() as conn:
        return await sends.insert(
            conn,
            session_id=session_id,
            slug=slug,
            kind=kind,
            room_id=room_id,
            text=text if kind == "message" else None,
            key=key if kind == "reaction" else None,
            reply_to=reply_to,
            ref_event_id=ref_event_id,
        )


async def send_state(pool, send_id) -> tuple:
    rows = await execute(pool, "SELECT status, reason FROM sends WHERE id = %s", send_id)
    return rows[0]


async def send_event_id(pool, send_id) -> str | None:
    rows = await execute(pool, "SELECT event_id FROM sends WHERE id = %s", send_id)
    return rows[0][0]


async def outbound_of(pool, send_id) -> list[tuple]:
    return await execute(
        pool,
        "SELECT kind, event_type, body, txn_id, relates_to, status, reason FROM outbound"
        " WHERE send_id = %s ORDER BY id",
        send_id,
    )


async def all_bindings(pool) -> list[tuple]:
    return await execute(
        pool,
        "SELECT id, room_id, session_id, slug, origin, send_id, released_at IS NOT NULL,"
        " release_reason FROM room_bindings ORDER BY id",
    )


async def active_binding(pool, room_id) -> tuple | None:
    rows = await execute(
        pool,
        "SELECT session_id, slug, origin, send_id FROM room_bindings"
        " WHERE room_id = %s AND released_at IS NULL",
        room_id,
    )
    return rows[0] if rows else None


async def deliver(sender, tracker):
    """Claim, send, settle: one sender tick, one tracker tick, one sender tick."""
    await sender.tick()
    await tracker.tick()
    await sender.tick()


def text_event(body: str) -> dict:
    return {"msgtype": "m.text", "body": body}


# --- pure: the content builder and the outbound plan (no database) ---------------------------


def _send(**fields) -> SendRow:
    values = dict(
        id=7,
        session_id=uuid.uuid4(),
        slug="hive-1",
        kind="message",
        room_id=R1,
        text="hello",
        key=None,
        reply_to=None,
        ref_event_id=None,
        event_id=None,
        status="sending",
        reason=None,
        created_at=T0,
        claimed_at=T0,
        sent_at=None,
    )
    values.update(fields)
    return SendRow(**values)


def test_event_content():
    assert event_content("m.room.message", "hi", None) == {"msgtype": "m.text", "body": "hi"}
    assert event_content("m.room.message", "hi", {"m.in_reply_to": {"event_id": "$e"}}) == {
        "msgtype": "m.text",
        "body": "hi",
        "m.relates_to": {"m.in_reply_to": {"event_id": "$e"}},
    }
    annotation = {"rel_type": "m.annotation", "event_id": "$e", "key": "\U0001f44d"}
    assert event_content("m.reaction", "\U0001f44d", annotation) == {"m.relates_to": annotation}
    with pytest.raises(ValueError):
        event_content("m.reaction", "\U0001f44d", None)
    with pytest.raises(ValueError):
        event_content("m.room.encrypted", "x", None)


def test_outbound_plan():
    first, second = "A" * 60, "B" * 60
    reply = _send(text=f"{first}\n\n{second}", reply_to="m3", ref_event_id="$target")
    assert outbound_plan(reply, 100) == Plan(
        kind="agent",
        event_type="m.room.message",
        chunks=[first, second],
        relates_to={"m.in_reply_to": {"event_id": "$target"}},
    )
    assert outbound_plan(_send(text="plain"), 100) == Plan(
        kind="agent", event_type="m.room.message", chunks=["plain"], relates_to=None
    )
    assert outbound_plan(_send(text=" \n\n "), 100).chunks == []
    reaction = _send(kind="reaction", text=None, key="\U0001f44d", reply_to="m3", ref_event_id="$t")
    assert outbound_plan(reaction, 100) == Plan(
        kind="reaction",
        event_type="m.reaction",
        chunks=["\U0001f44d"],
        relates_to={"rel_type": "m.annotation", "event_id": "$t", "key": "\U0001f44d"},
    )


async def test_bot_loop_runs_sender_between_worker_and_tracker(caplog):
    calls: list[str] = []
    stop = asyncio.Event()
    settings = SimpleNamespace(poll_seconds=0.001)

    class Worker:
        async def tick(self):
            calls.append("worker")

    class Sender:
        def __init__(self, error):
            self.error = error

        async def tick(self):
            calls.append("sender")
            if self.error is not None:
                error, self.error = self.error, None
                raise error

    class Tracker:
        async def tick(self):
            calls.append("tracker")
            if calls.count("tracker") == 2:
                stop.set()

    await asyncio.wait_for(
        bot._work_loop(Worker(), Sender(RuntimeError("boom")), Tracker(), settings, stop), 5
    )
    assert calls == ["worker", "sender", "tracker"] * 2
    assert "Sender failed; retrying next interval" in caplog.text

    calls.clear()
    stop.clear()
    lost = MatrixAuthError(401, "M_UNKNOWN_TOKEN", "gone")
    with pytest.raises(MatrixAuthError):
        await asyncio.wait_for(bot._work_loop(Worker(), Sender(lost), Tracker(), settings, stop), 5)
    assert calls == ["worker", "sender"]


# --- database-backed ---------------------------------------------------------------------------


async def test_message_send_chunks_and_reply_relation(sender, tracker, pool, hs, matrix):
    session = uuid.uuid4()
    await allowed_room(pool, R1)
    await bind(pool, R1, session)
    before = await all_bindings(pool)
    a, b, c = "A" * 60, "B" * 60, "C" * 60
    reply = await queue(
        pool, session, R1, text=f"{a}\n\n{b}\n\n{c}", reply_to="m4", ref_event_id="$target"
    )
    plain = await queue(pool, session, R1, text="follow-up")

    await sender.tick()
    relation = {"m.in_reply_to": {"event_id": "$target"}}
    assert await send_state(pool, reply) == ("sending", None)
    assert await outbound_of(pool, reply) == [
        ("agent", "m.room.message", a, f"mxa-{reply}-1", relation, "pending", None),
        ("agent", "m.room.message", b, f"mxa-{reply}-2", None, "pending", None),
        ("agent", "m.room.message", c, f"mxa-{reply}-3", None, "pending", None),
    ]
    assert await outbound_of(pool, plain) == [
        ("agent", "m.room.message", "follow-up", f"mxa-{plain}-1", None, "pending", None)
    ]

    await tracker.tick()
    assert hs.sent(R1) == [
        {**text_event(a), "m.relates_to": relation},
        text_event(b),
        text_event(c),
        text_event("follow-up"),
    ]
    assert [(room, kind, txn) for room, kind, txn, _ in matrix.events] == [
        (R1, "m.room.message", f"mxa-{reply}-1"),
        (R1, "m.room.message", f"mxa-{reply}-2"),
        (R1, "m.room.message", f"mxa-{reply}-3"),
        (R1, "m.room.message", f"mxa-{plain}-1"),
    ]
    await sender.tick()
    assert await send_state(pool, reply) == ("sent", None)
    assert await send_state(pool, plain) == ("sent", None)
    assert await all_bindings(pool) == before


async def test_reaction_send(sender, tracker, pool, hs, matrix):
    session = uuid.uuid4()
    await allowed_room(pool, R1)
    await bind(pool, R1, session)
    thumbs = "\U0001f44d"
    reaction = await queue(
        pool, session, R1, kind="reaction", key=thumbs, reply_to="m4", ref_event_id="$target"
    )
    annotation = {"rel_type": "m.annotation", "event_id": "$target", "key": thumbs}

    await sender.tick()
    assert await outbound_of(pool, reaction) == [
        ("reaction", "m.reaction", thumbs, f"mxa-{reaction}-r", annotation, "pending", None)
    ]
    await tracker.tick()
    assert hs.sent(R1) == [{"m.relates_to": annotation}]
    assert matrix.events == [(R1, "m.reaction", f"mxa-{reaction}-r", {"m.relates_to": annotation})]
    await sender.tick()
    assert await send_state(pool, reaction) == ("sent", None)
    rows = await execute(pool, "SELECT event_id FROM outbound WHERE send_id = %s", reaction)
    assert await send_event_id(pool, reaction) == rows[0][0] is not None


async def test_policy_recheck_refuses(sender, tracker, pool, hs):
    denied_member, rule_removed, by_facebook, flagged = (uuid.uuid4() for _ in range(4))
    await allowed_room(pool, R1, members=(ALICE, BOB))
    await bind(pool, R1, denied_member)
    await add_room(pool, R2, members=(CAROL,))  # no rule names CAROL
    removed_rule = await add_rule(pool, "allow", "room", R2)
    await bind(pool, R2, rule_removed)
    await add_room(pool, R3)
    await add_rule(pool, "allow", "facebook", "1")  # ALICE's facebook id: R3 is allowed only so
    await bind(pool, R3, by_facebook)
    await allowed_room(pool, R4, members=(BOB,), flag=True)  # unbound, flagged

    denied_send = await queue(pool, denied_member, R1)
    removed_send = await queue(pool, rule_removed, R2)
    facebook_send = await queue(pool, by_facebook, R3, text="via facebook")
    flagged_send = await queue(pool, flagged, R4)
    # The api accepted all four; then a member is denied and a room's allow rule removed.
    await add_rule(pool, "deny", "user", BOB)
    await execute(pool, "DELETE FROM access_rules WHERE id = %s", removed_rule)
    before = await all_bindings(pool)

    await deliver(sender, tracker)
    assert await send_state(pool, denied_send) == ("refused", "denied: user")
    assert await send_state(pool, removed_send) == ("refused", "not_allowed")
    assert await send_state(pool, flagged_send) == ("refused", "denied: user")
    for refused in (denied_send, removed_send, flagged_send):
        assert await outbound_of(pool, refused) == []
    assert hs.sent(R1) == hs.sent(R2) == hs.sent(R4) == []
    assert await all_bindings(pool) == before

    assert await send_state(pool, facebook_send) == ("sent", None)
    assert hs.sent(R3) == [text_event("via facebook")]


async def test_claim_binds_unbound_flagged_room(sender, tracker, pool, hs, monkeypatch):
    session = uuid.uuid4()
    await allowed_room(pool, R1, flag=True)
    send_id = await queue(pool, session, R1, text="hello there", slug="hive-7")

    async def crash(*args, **kwargs):
        raise RuntimeError("crash while storing the outbound rows")

    real_add = runs.add_outbound
    monkeypatch.setattr(runs, "add_outbound", crash)
    with pytest.raises(RuntimeError, match="crash while storing"):
        await sender.tick()
    # One transaction: the claim, the binding and the rows roll back together.
    assert await send_state(pool, send_id) == ("queued", None)
    assert await all_bindings(pool) == []

    monkeypatch.setattr(runs, "add_outbound", real_add)
    await sender.tick()
    assert await active_binding(pool, R1) == (session, "hive-7", "agent", send_id)
    assert [row[3] for row in await outbound_of(pool, send_id)] == [f"mxa-{send_id}-1"]
    await tracker.tick()
    await sender.tick()
    assert await send_state(pool, send_id) == ("sent", None)
    assert hs.sent(R1) == [text_event("hello there")]
    assert await active_binding(pool, R1) == (session, "hive-7", "agent", send_id)
    assert len(await all_bindings(pool)) == 1


async def test_claim_refuses_binding_conflicts(sender, tracker, pool, hs):
    s1, s2, s3, s4, s5a, s5b, other = (uuid.uuid4() for _ in range(7))
    for room in (R1, R2, R3, R5, R6):
        await allowed_room(pool, room, flag=True)
    await allowed_room(pool, R4, flag=True)

    room_bound = await queue(pool, s1, R1)
    reserved = await queue(pool, s2, R2)
    session_bound = await queue(pool, s3, R3)
    not_authorised = await queue(pool, s4, R4)
    first = await queue(pool, s5a, R5, text="first")
    second = await queue(pool, s5b, R5, text="second")
    # After the api accepted them: R1 bound to another session, R2 reserved by a first run,
    # s3 bound to another room, R4 no longer flagged.
    await bind(pool, R1, other)
    await reserve(pool, R2)
    await bind(pool, R6, s3)
    await set_flag(pool, R4, False)
    before = await all_bindings(pool)

    await deliver(sender, tracker)
    assert await send_state(pool, room_bound) == ("refused", "room_bound")
    assert await send_state(pool, reserved) == ("refused", "room_bound")
    assert await send_state(pool, session_bound) == ("refused", "session_bound")
    assert await send_state(pool, not_authorised) == ("refused", "room_not_authorised")
    assert await send_state(pool, second) == ("refused", "room_bound")
    for refused in (room_bound, reserved, session_bound, not_authorised, second):
        assert await outbound_of(pool, refused) == []
    for room in (R1, R2, R3, R4, R6):
        assert hs.sent(room) == []

    assert await send_state(pool, first) == ("sent", None)
    assert hs.sent(R5) == [text_event("first")]
    after = await all_bindings(pool)
    assert after[: len(before)] == before
    assert [row[1:] for row in after[len(before) :]] == [
        (R5, s5a, "hive-1", "agent", first, False, None)
    ]


async def test_failed_initiation_releases_binding(sender, tracker, pool, hs, matrix):
    failed_init, partly_sent, refused_init, bound_session = (uuid.uuid4() for _ in range(4))
    await allowed_room(pool, R1, flag=True)
    await allowed_room(pool, R2, flag=True)
    await add_room(pool, R3, members=(BOB,), flag=True)
    await add_rule(pool, "allow", "room", R3)
    await allowed_room(pool, R4)
    await bind(pool, R4, bound_session)

    failed = await queue(pool, failed_init, R1, text="never arrives")
    partial = await queue(pool, partly_sent, R2, text=f"{'A' * 60}\n\n{'B' * 60}")
    refused = await queue(pool, refused_init, R3, text="denied before sending")
    into_bound = await queue(pool, bound_session, R4, text="fails too")

    await sender.tick()
    for room, session, send_id in (
        (R1, failed_init, failed),
        (R2, partly_sent, partial),
        (R3, refused_init, refused),
    ):
        assert await active_binding(pool, room) == (session, "hive-1", "agent", send_id)
    matrix.fail[f"mxa-{failed}-1"] = MatrixError(400, "M_TOO_LARGE", "too large")
    matrix.fail[f"mxa-{partial}-2"] = MatrixError(400, "M_TOO_LARGE", "too large")
    matrix.fail[f"mxa-{into_bound}-1"] = MatrixError(400, "M_TOO_LARGE", "too large")
    await add_rule(pool, "deny", "user", BOB)  # R3's only member: refused at send time
    await tracker.tick()
    await sender.tick()

    assert await send_state(pool, failed) == ("failed", "M_TOO_LARGE")
    assert await send_state(pool, partial) == ("failed", "M_TOO_LARGE")
    assert await send_state(pool, refused) == ("refused", "denied: user")
    assert await send_state(pool, into_bound) == ("failed", "M_TOO_LARGE")
    assert hs.sent(R2) == [text_event("A" * 60)]
    assert hs.sent(R1) == hs.sent(R3) == hs.sent(R4) == []

    released = {row[1]: (row[5], row[6], row[7]) for row in await all_bindings(pool) if row[6]}
    assert released == {R1: (failed, True, "send_failed"), R3: (refused, True, "send_failed")}
    assert await active_binding(pool, R1) is None
    assert await active_binding(pool, R3) is None
    assert await active_binding(pool, R2) == (partly_sent, "hive-1", "agent", partial)
    assert await active_binding(pool, R4) == (bound_session, "earlier", "agent", 999)


async def test_settle_sent_and_failed(sender, tracker, pool, hs, matrix):
    session = uuid.uuid4()
    await allowed_room(pool, R1)
    await bind(pool, R1, session)
    a, b, c = "A" * 60, "B" * 60, "C" * 60
    all_sent = await queue(pool, session, R1, text=f"{a}\n\n{b}")
    one_failed = await queue(pool, session, R1, text=f"{a}\n\n{b}\n\n{c}")

    await sender.tick()
    await sender.tick()  # rows still pending: nothing settles
    assert await send_state(pool, all_sent) == ("sending", None)
    assert await send_state(pool, one_failed) == ("sending", None)

    matrix.fail[f"mxa-{one_failed}-2"] = MatrixError(400, "M_TOO_LARGE", "too large")
    matrix.fail[f"mxa-{one_failed}-3"] = MatrixError(400, "M_BAD_JSON", "bad")
    await tracker.tick()
    await sender.tick()

    assert await send_state(pool, all_sent) == ("sent", None)
    first_row = await execute(
        pool, "SELECT event_id FROM outbound WHERE send_id = %s ORDER BY id LIMIT 1", all_sent
    )
    assert await send_event_id(pool, all_sent) == first_row[0][0] is not None
    sent_at = await execute(pool, "SELECT sent_at FROM sends WHERE id = %s", all_sent)
    assert sent_at[0][0] is not None
    assert await send_state(pool, one_failed) == ("failed", "M_TOO_LARGE")
    assert await send_event_id(pool, one_failed) is None
    assert [row[5:] for row in await outbound_of(pool, one_failed)] == [
        ("sent", None),
        ("failed", "M_TOO_LARGE"),
        ("failed", "M_BAD_JSON"),
    ]


async def test_sends_go_out_during_running_chat_run(sender, tracker, pool, hs, orch, orch_client):
    preset = orch.add_preset("chat")
    session = await orch_client.create_session(preset.id, "mx-chat")
    message = await orch_client.post_message(session.id, "prompt")
    await allowed_room(pool, R1)
    async with pool.connection() as conn:
        ref = await runs.create_run(conn, room_id=R1, preset_id=str(preset.id), kind="first")
        await inbound.insert(
            conn,
            event_id="$hi",
            room_id=R1,
            sender=ALICE,
            sender_name="Alice",
            body="hi",
            mentioned=False,
            origin_ts=None,
            status="new",
            reason=None,
        )
        await inbound.claim_room(conn, R1, ref.id)
        await conn.execute(
            "UPDATE runs SET session_id = %s, message_id = %s, started_at = %s WHERE id = %s",
            (session.id, str(message.id), T0, ref.id),
        )
        await conn.execute(
            "INSERT INTO room_bindings (room_id, session_id, slug, origin, run_id)"
            " VALUES (%s, %s, 'mx-chat', 'inbound', %s)",
            (R1, session.id, ref.id),
        )
    one = await queue(pool, session.id, R1, text="working on it")
    two = await queue(pool, session.id, R1, text="still working")

    await deliver(sender, tracker)
    assert hs.sent(R1) == [text_event("working on it"), text_event("still working")]
    assert await send_state(pool, one) == ("sent", None)
    assert await send_state(pool, two) == ("sent", None)
    rows = await execute(pool, "SELECT status FROM runs WHERE id = %s", ref.id)
    assert rows == [("in-progress",)]

    orch.complete_turn(message.id, "done")
    await tracker.tick()
    assert hs.sent(R1)[-1] == text_event("done")


async def test_restart_exactly_once(
    sender, tracker, pool, hs, matrix, orch_client, clock, monkeypatch
):
    session = uuid.uuid4()
    await allowed_room(pool, R1)
    await bind(pool, R1, session)
    message = await queue(pool, session, R1, text=f"{'A' * 60}\n\n{'B' * 60}")
    reaction = await queue(
        pool, session, R1, kind="reaction", key="❤", reply_to="m1", ref_event_id="$x"
    )
    await sender.tick()

    async def crash(conn, outbound_id, event_id):
        raise RuntimeError("crash before mark_sent")

    real_mark_sent = runs.mark_sent
    monkeypatch.setattr(runs, "mark_sent", crash)
    with pytest.raises(RuntimeError, match="crash before mark_sent"):
        await tracker.tick()
    assert hs.sent(R1) == [text_event("A" * 60)]
    monkeypatch.setattr(runs, "mark_sent", real_mark_sent)

    restarted_sender = Sender(matrix, pool, make_settings(), now=clock)
    restarted = Tracker(orch_client, matrix, pool, make_settings(), now=clock)
    await restarted_sender.tick()
    await restarted.tick()
    await restarted_sender.tick()
    assert [txn for _, _, txn, _ in matrix.events] == [
        f"mxa-{message}-1",
        f"mxa-{message}-1",
        f"mxa-{message}-2",
        f"mxa-{reaction}-r",
    ]
    assert hs.sent(R1) == [
        text_event("A" * 60),
        text_event("B" * 60),
        {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$x", "key": "❤"}},
    ]
    assert await send_state(pool, message) == ("sent", None)
    assert await send_state(pool, reaction) == ("sent", None)
    await restarted.tick()
    assert len(matrix.events) == 4


async def test_daily_prune_includes_sends(tracker, pool, clock):
    old = T0 - timedelta(days=31)
    recent = T0 - timedelta(days=28)
    session = uuid.uuid4()
    for tag, at in (("old", old), ("recent", recent)):
        for status in ("queued", "sending", "sent", "failed", "refused"):
            await execute(
                pool,
                "INSERT INTO sends (session_id, kind, room_id, text, status, reason, created_at)"
                " VALUES (%s, 'message', %s, 'x', %s, %s, %s)",
                session,
                R1,
                status,
                tag,
                at,
            )
        for released in (True, False):
            await execute(
                pool,
                "INSERT INTO room_bindings (room_id, session_id, origin, send_id, created_at,"
                " released_at, release_reason) VALUES (%s, %s, 'agent', 1, %s, %s, %s)",
                f"!{tag}-{released}:hs",
                uuid.uuid4(),
                at,
                at if released else None,
                tag if released else None,
            )

    await tracker.tick()
    assert await execute(pool, "SELECT reason, status FROM sends ORDER BY id") == [
        ("old", "queued"),
        ("old", "sending"),
        ("recent", "queued"),
        ("recent", "sending"),
        ("recent", "sent"),
        ("recent", "failed"),
        ("recent", "refused"),
    ]
    assert await execute(pool, "SELECT room_id FROM room_bindings ORDER BY id") == [
        ("!old-False:hs",),
        ("!recent-True:hs",),
        ("!recent-False:hs",),
    ]


async def test_whitespace_message_fails_without_rows_or_binding(sender, tracker, pool, hs):
    session = uuid.uuid4()
    await allowed_room(pool, R1, flag=True)
    blank = await queue(pool, session, R1, text=" \n\n \t")

    await deliver(sender, tracker)
    assert await send_state(pool, blank) == ("failed", "empty_text")
    assert await outbound_of(pool, blank) == []
    assert await all_bindings(pool) == []
    assert hs.sent(R1) == []
