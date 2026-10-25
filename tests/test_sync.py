import asyncio
import logging
from datetime import UTC, datetime

import httpx
import pytest
from agents_common.db.pool import open_pool

from agents_matrix_connector import sync as sync_module
from agents_matrix_connector.db import inbound, rules
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import MatrixAuthError, MatrixClient, MatrixForbidden
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.sync import SyncLoop

PACKAGE = "agents_matrix_connector"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TOKEN = "syt_sync_test_token"
BOT = "@hermes:hs"
SELF_FB = "@facebook_999:hs"
BRIDGE = "@facebookbot:hs"
ALICE = "@facebook_1:hs"
BOB = "@facebook_2:hs"
MALLORY = "@facebook_3:hs"
DM = "!dm:hs"
GROUP = "!group:hs"
TYPES = [
    "m.room.message",
    "m.room.member",
    "m.room.name",
    "m.room.canonical_alias",
    "m.room.encryption",
    "m.bridge",
    "uk.half-shot.bridge",
]


def bridge_state(room_type: str, displayname: str | None = None) -> dict:
    channel = {"id": "thread"}
    if displayname is not None:
        channel["displayname"] = displayname
    return {
        "type": "m.bridge",
        "state_key": "fb-thread",
        "content": {"com.beeper.room_type.v2": room_type, "channel": channel},
    }


def text(body: str, **extra) -> dict:
    return {"msgtype": "m.text", "body": body, **extra}


def make_settings(**overrides) -> BotSettings:
    values = dict(
        database_url="postgresql://unused/x",
        orchestrator_url="http://orch:8000",
        orchestrator_token="orch-token",
        homeserver_url="http://hs",
        user_id=BOT,
        self_identities=[SELF_FB],
        bridge_bots=[BRIDGE],
        access_token=TOKEN,
        sync_timeout_seconds=1,
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
def fake() -> FakeHomeserver:
    return FakeHomeserver(BOT, TOKEN)


@pytest.fixture
async def client(fake):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=fake.app)) as c:
        yield c


def make_loop(client, pool, **overrides) -> SyncLoop:
    return SyncLoop(client, pool, make_settings(**overrides), now=lambda: T0)


async def fetch(pool, query: str, *args) -> list[tuple]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, args)
        return await cur.fetchall()


async def inbound_rows(pool) -> list[tuple]:
    return await fetch(
        pool, "SELECT sender, status, reason, body, mentioned FROM inbound ORDER BY id"
    )


async def sync_state(pool) -> tuple:
    return (await fetch(pool, "SELECT next_batch, filter_id, last_sync_at FROM sync_state"))[0]


def add_dm(fake: FakeHomeserver, *, joined: bool = True, room_id: str = DM, state=None) -> None:
    members = {BRIDGE: "Facebook bridge", ALICE: "Alice", SELF_FB: "Hermes"}
    if joined:
        members[BOT] = "hermes"
    fake.add_room(room_id, state if state is not None else [bridge_state("dm", "Alice")], members)


async def joined_and_synced(fake, client, pool, **overrides) -> SyncLoop:
    """A DM portal the bot is in, after the first sync (history is never processed)."""
    add_dm(fake)
    loop = make_loop(client, pool, **overrides)
    await loop.run_once()
    return loop


async def test_invite_join_and_classify(fake, client, pool, monkeypatch, caplog):
    created: list[dict] = []
    real_create = client.create_filter

    async def spy_create(user_id, definition):
        created.append(definition)
        return await real_create(user_id, definition)

    monkeypatch.setattr(client, "create_filter", spy_create)
    add_dm(fake, joined=False)
    fake.invite(DM, BRIDGE)
    fake.fail_next("join", 500)
    loop = make_loop(client, pool)

    with caplog.at_level(logging.WARNING, logger="agents_matrix_connector.sync"):
        await loop.run_once()
    assert await fetch(pool, "SELECT membership, inviter, kind FROM rooms") == [
        ("invite", BRIDGE, None)
    ]
    assert DM in caplog.text
    with pytest.raises(MatrixForbidden):
        await client.joined_members(DM)
    assert loop.last_sync_at == T0

    [definition] = created
    assert definition["room"]["timeline"]["types"] == TYPES
    assert definition["room"]["state"]["types"] == TYPES
    assert definition["presence"] == {"not_types": ["*"]}
    assert definition["account_data"] == {"not_types": ["*"]}
    assert definition["room"]["ephemeral"] == {"not_types": ["*"]}
    assert definition["room"]["account_data"] == {"not_types": ["*"]}
    next_batch, filter_id, _ = await sync_state(pool)
    assert filter_id == "0" and next_batch is not None

    # The failed join is retried on the next sync because the invite stays pending.
    await loop.run_once()
    assert BOT in await client.joined_members(DM)

    await loop.run_once()
    assert await fetch(
        pool, "SELECT membership, inviter, kind, name, member_count, message_request FROM rooms"
    ) == [("join", BRIDGE, "dm", "Alice", 4, False)]
    assert await fetch(
        pool,
        "SELECT m.user_id, m.membership, m.display_name, u.facebook_id"
        " FROM room_members m JOIN users u USING (user_id) ORDER BY m.user_id",
    ) == [
        (ALICE, "join", "Alice", "1"),
        (SELF_FB, "join", "Hermes", "999"),
        (BRIDGE, "join", "Facebook bridge", None),
        (BOT, "join", None, None),
    ]
    assert len(created) == 1

    # A restarted loop reuses the stored filter.
    await make_loop(client, pool).run_once()
    assert len(created) == 1


async def test_first_sync_no_replay(fake, client, pool):
    add_dm(fake)
    fake.post(DM, ALICE, text("old message one"))
    fake.post(DM, ALICE, text("old message two"))
    await rules.add_rule(pool, "allow", "room", DM, None)
    loop = make_loop(client, pool)

    await loop.run_once()
    assert await fetch(pool, "SELECT membership, kind, name, last_event_at FROM rooms") == [
        ("join", "dm", "Alice", datetime.fromtimestamp((1_700_000_000_000 + 9) / 1000, UTC))
    ]
    assert len(await fetch(pool, "SELECT * FROM room_members")) == 4
    assert (await fetch(pool, "SELECT facebook_id FROM users WHERE user_id = %s", ALICE)) == [
        ("1",)
    ]
    assert await inbound_rows(pool) == []
    assert (await sync_state(pool))[0] is not None

    fake.post(DM, ALICE, text("new message"))
    await loop.run_once()
    assert await inbound_rows(pool) == [(ALICE, "new", None, "new message", False)]


async def test_default_deny_then_allow(fake, client, pool):
    loop = await joined_and_synced(fake, client, pool, max_inbound_chars=100)

    fake.post(DM, ALICE, text("x" * 150))
    await loop.run_once()
    assert await inbound_rows(pool) == [
        (ALICE, "ignored", "not_allowed", "x" * 100 + " [truncated]", False)
    ]

    await rules.add_rule(pool, "allow", "room", DM, None)
    fake.post(DM, ALICE, text("hello agent"))
    await loop.run_once()
    rows = await inbound_rows(pool)
    assert rows[1] == (ALICE, "new", None, "hello agent", False)
    assert await fetch(pool, "SELECT sender_name FROM inbound ORDER BY id") == [
        ("Alice",),
        ("Alice",),
    ]


async def test_denied_and_self_messages(fake, client, pool):
    add_dm(fake)
    fake.set_member(DM, BOB, "join", "Bob")
    fake.set_member(DM, MALLORY, "join", "Mallory")
    loop = make_loop(client, pool)
    await loop.run_once()
    await rules.add_rule(pool, "allow", "room", DM, None)
    await rules.add_rule(pool, "deny", "facebook", "1", None)
    await rules.add_rule(pool, "deny", "display_name", "mallory", None)

    fake.post(DM, ALICE, text("secret from alice"))
    fake.post(DM, MALLORY, text("secret from mallory"))
    fake.post(DM, BOB, text("hi from bob"))
    fake.post(DM, BOT, text("bot reply"))
    fake.post(DM, SELF_FB, text("echo of bot reply"))
    fake.post(DM, BRIDGE, text("bridge notice"))
    await loop.run_once()

    assert await inbound_rows(pool) == [
        (ALICE, "ignored", "denied: facebook", None, False),
        (MALLORY, "ignored", "denied: display_name", None, False),
        (BOB, "new", None, "hi from bob", False),
    ]


async def test_group_mention_trigger(fake, client, pool):
    fake.add_room(
        GROUP,
        [bridge_state("group_dm", "Team")],
        {BRIDGE: "bridge", ALICE: "Alice", BOB: "Bob", SELF_FB: "Hermes Bot", BOT: "hermes"},
    )
    loop = make_loop(client, pool)
    await loop.run_once()
    await rules.add_rule(pool, "allow", "room", GROUP, None)

    fake.post(GROUP, ALICE, text("hello everyone"))
    fake.post(GROUP, ALICE, text("hey hermes bot, a question"))
    fake.post(GROUP, BOB, text("ping", **{"m.mentions": {"user_ids": [SELF_FB]}}))
    await loop.run_once()
    assert await inbound_rows(pool) == [
        (ALICE, "ignored", "not_mentioned", "hello everyone", False),
        (ALICE, "new", None, "hey hermes bot, a question", True),
        (BOB, "new", None, "ping", True),
    ]

    await rules.set_room_settings(pool, GROUP, preset_id=None, trigger="all")
    fake.post(GROUP, BOB, text("no mention now"))
    await loop.run_once()
    assert (await inbound_rows(pool))[3] == (BOB, "new", None, "no mention now", False)


async def test_encrypted_infrastructure_unsupported(fake, client, pool):
    enc, infra = "!enc:hs", "!infra:hs"
    add_dm(fake)
    add_dm(
        fake,
        room_id=enc,
        state=[
            bridge_state("dm", "Enc"),
            {"type": "m.room.encryption", "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    )
    add_dm(fake, room_id=infra, state=[bridge_state("space", "Space")])
    loop = make_loop(client, pool)
    await loop.run_once()
    for room in (DM, enc, infra):
        await rules.add_rule(pool, "allow", "room", room, None)

    fake.post(enc, ALICE, text("encrypted room text"))
    fake.post(infra, ALICE, text("space text"))
    fake.post(DM, ALICE, {"msgtype": "m.image", "body": "photo.jpg", "url": "mxc://hs/x"})
    await loop.run_once()
    assert sorted(await fetch(pool, "SELECT room_id, status, reason, body FROM inbound")) == sorted(
        [
            (enc, "ignored", "room_encrypted", None),
            (infra, "ignored", "room_infrastructure", None),
            (DM, "ignored", "unsupported_content", None),
        ]
    )

    # Encryption turned on later updates the classified room.
    fake.encrypt(DM)
    fake.post(DM, ALICE, text("after encryption"))
    await loop.run_once()
    assert await fetch(pool, "SELECT encrypted FROM rooms WHERE room_id = %s", DM) == [(True,)]
    assert await fetch(
        pool, "SELECT status, reason, body FROM inbound WHERE room_id = %s ORDER BY id DESC", DM
    ) == [("ignored", "room_encrypted", None), ("ignored", "unsupported_content", None)]


async def test_queue_cap(fake, client, pool):
    loop = await joined_and_synced(fake, client, pool, max_queued_per_room=2)
    await rules.add_rule(pool, "allow", "room", DM, None)

    for n in range(3):
        fake.post(DM, ALICE, text(f"message {n}"))
    await loop.run_once()
    assert await inbound_rows(pool) == [
        (ALICE, "new", None, "message 0", False),
        (ALICE, "new", None, "message 1", False),
        (ALICE, "ignored", "queue_full", "message 2", False),
    ]


async def test_since_token_and_rows_commit_together(fake, client, pool, monkeypatch):
    await joined_and_synced(fake, client, pool)
    await rules.add_rule(pool, "allow", "room", DM, None)
    before = await sync_state(pool)
    fake.post(DM, ALICE, text("first"))
    fake.post(DM, ALICE, text("second"))

    real_insert = inbound.insert
    calls = {"n": 0}

    async def crash_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("crash before commit")
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(inbound, "insert", crash_on_second)
    crash_loop = SyncLoop(
        client, pool, make_settings(), now=lambda: datetime(2030, 1, 1, tzinfo=UTC)
    )
    with pytest.raises(RuntimeError, match="crash before commit"):
        await crash_loop.run_once()
    assert await inbound_rows(pool) == []
    assert await sync_state(pool) == before
    assert crash_loop.last_sync_at is None

    await crash_loop.run_once()
    rows = await inbound_rows(pool)
    assert rows == [(ALICE, "new", None, "first", False), (ALICE, "new", None, "second", False)]
    after = await sync_state(pool)
    assert after[0] != before[0]
    assert after[2] == datetime(2030, 1, 1, tzinfo=UTC)

    # Replaying the same response (since token rewound) adds no duplicate rows.
    async with pool.connection() as conn:
        await conn.execute("UPDATE sync_state SET next_batch = %s", (before[0],))
    await crash_loop.run_once()
    assert await inbound_rows(pool) == rows
    assert (await sync_state(pool))[0] == after[0]


async def test_backoff_and_auth_error(fake, client, pool, monkeypatch):
    delays: list[float] = []

    async def record_pause(stop, seconds):
        delays.append(seconds)

    monkeypatch.setattr(sync_module, "_pause", record_pause)
    script = ["fail"] * 8 + ["ok", "fail", "revoke"]
    real_sync = client.sync

    async def scripted(*args, **kwargs):
        step = script.pop(0)
        if step == "fail":
            fake.fail_next("sync", 503)
        elif step == "revoke":
            fake.revoke_token()
        return await real_sync(*args, **kwargs)

    monkeypatch.setattr(client, "sync", scripted)
    loop = make_loop(client, pool)
    with pytest.raises(MatrixAuthError):
        await asyncio.wait_for(loop.run(asyncio.Event()), 30)
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 1]
    assert script == []

    # A set stop event ends the loop after the current sync (on a fresh homeserver).
    async with pool.connection() as conn:
        await conn.execute("UPDATE sync_state SET next_batch = NULL, filter_id = NULL")
    stop = asyncio.Event()
    calls = {"n": 0}
    fresh = FakeHomeserver(BOT, TOKEN)
    async with MatrixClient(
        "http://hs", TOKEN, transport=httpx.ASGITransport(app=fresh.app)
    ) as other:
        real_other = other.sync

        async def stopping(*args, **kwargs):
            calls["n"] += 1
            stop.set()
            return await real_other(*args, **kwargs)

        monkeypatch.setattr(other, "sync", stopping)
        await asyncio.wait_for(make_loop(other, pool).run(stop), 30)
    assert calls["n"] == 1


async def test_pre_join_history_skipped(fake, client, pool):
    loop = make_loop(client, pool)
    await loop.run_once()
    add_dm(fake, joined=False)
    await rules.add_rule(pool, "allow", "room", DM, None)
    fake.post(DM, ALICE, text("before invite"))
    fake.invite(DM, BRIDGE)
    fake.post(DM, ALICE, text("while invited"))
    await client.join(DM)
    fake.post(DM, ALICE, text("after join"))

    await loop.run_once()
    assert await inbound_rows(pool) == [(ALICE, "new", None, "after join", False)]

    # Once joined, a later profile change of the bot does not hide messages before it.
    fake.post(DM, ALICE, text("before rename"))
    fake.set_member(DM, BOT, "join", "hermes renamed")
    await loop.run_once()
    assert [r[3] for r in await inbound_rows(pool)] == ["after join", "before rename"]


async def test_pre_join_history_by_timestamp(fake, client, pool, monkeypatch):
    add_dm(fake)
    await rules.add_rule(pool, "allow", "room", DM, None)
    async with pool.connection() as conn:
        await conn.execute("UPDATE sync_state SET next_batch = 's0'")
    join_ts = 1_800_000_000_000

    def message(event_id: str, ts: int, body: str) -> dict:
        return {
            "event_id": event_id,
            "type": "m.room.message",
            "sender": ALICE,
            "origin_server_ts": ts,
            "content": text(body),
        }

    response = {
        "next_batch": "s100",
        "rooms": {
            "join": {
                DM: {
                    "state": {
                        "events": [
                            {
                                "event_id": "$join",
                                "type": "m.room.member",
                                "state_key": BOT,
                                "sender": BOT,
                                "origin_server_ts": join_ts,
                                "content": {"membership": "join"},
                            }
                        ]
                    },
                    "timeline": {
                        "events": [
                            message("$old", join_ts - 1, "history"),
                            message("$new", join_ts + 1, "fresh"),
                        ],
                        "limited": True,
                    },
                }
            }
        },
    }

    async def handcrafted(*args, **kwargs):
        return response

    monkeypatch.setattr(client, "sync", handcrafted)
    await make_loop(client, pool).run_once()
    assert await inbound_rows(pool) == [(ALICE, "new", None, "fresh", False)]


async def test_lost_filter_recreated(fake, client, pool, monkeypatch):
    await joined_and_synced(fake, client, pool)
    await rules.add_rule(pool, "allow", "room", DM, None)
    async with pool.connection() as conn:
        await conn.execute("UPDATE sync_state SET filter_id = 'lost'")
    created: list[str] = []
    real_create = client.create_filter

    async def spy_create(user_id, definition):
        created.append(await real_create(user_id, definition))
        return created[-1]

    monkeypatch.setattr(client, "create_filter", spy_create)
    fake.post(DM, ALICE, text("after the filter was lost"))

    restarted = make_loop(client, pool)
    await restarted.run_once()
    assert created == ["1"]
    assert (await sync_state(pool))[1] == "1"
    assert await inbound_rows(pool) == [(ALICE, "new", None, "after the filter was lost", False)]

    fake.post(DM, ALICE, text("next"))
    await restarted.run_once()
    assert created == ["1"]
    assert len(await inbound_rows(pool)) == 2


LATE = "!late:hs"
FRESH = "!fresh:hs"


def spy_messages(monkeypatch, client) -> list[tuple]:
    """Record (room_id, from_token, limit, chunk length) of every room_messages call."""
    calls: list[tuple] = []
    real = client.room_messages

    async def spy(room_id, from_token, **kwargs):
        page = await real(room_id, from_token, **kwargs)
        calls.append((room_id, from_token, kwargs.get("limit"), len(page.get("chunk", []))))
        return page

    monkeypatch.setattr(client, "room_messages", spy)
    return calls


async def bodies(pool) -> list[str]:
    return [row[3] for row in await inbound_rows(pool)]


async def test_limited_timeline_backfill(fake, client, pool, monkeypatch):
    loop = await joined_and_synced(fake, client, pool)
    await rules.add_rule(pool, "allow", "room", DM, None)
    fake.post(DM, ALICE, text("before downtime"))
    await loop.run_once()
    before = await sync_state(pool)

    monkeypatch.setattr(sync_module, "BACKFILL_PAGE", 2)
    responses: list[dict] = []
    real_sync = client.sync

    async def keep_response(*args, **kwargs):
        responses.append(await real_sync(*args, **kwargs))
        return responses[-1]

    monkeypatch.setattr(client, "sync", keep_response)
    calls = spy_messages(monkeypatch, client)
    fake.limit_timeline(2)
    fake.post(DM, BOT, text("bot reply"))
    for n in range(5):
        fake.post(DM, ALICE, text(f"missed {n}"))

    real_insert = inbound.insert
    inserted: list[str] = []
    crash = {"armed": True}

    async def recording_insert(*args, **kwargs):
        if crash["armed"] and inserted:
            raise RuntimeError("crash before commit")
        inserted.append(kwargs["event_id"])
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(inbound, "insert", recording_insert)
    with pytest.raises(RuntimeError, match="crash before commit"):
        await loop.run_once()
    assert await bodies(pool) == ["before downtime"]
    assert await sync_state(pool) == before

    crash["armed"] = False
    inserted.clear()
    calls.clear()
    await loop.run_once()
    assert await bodies(pool) == ["before downtime"] + [f"missed {n}" for n in range(5)]
    assert len(inserted) == len(set(inserted)) == 5
    [_, response] = responses
    timeline = response["rooms"]["join"][DM]["timeline"]
    assert timeline["limited"] is True
    assert [c[:3] for c in calls[:1]] == [(DM, timeline["prev_batch"], 2)]
    assert [c[2] for c in calls] == [2, 2, 2]
    assert (await sync_state(pool))[0] == response["next_batch"]

    # A sync that is not limited reads no history.
    fake.limit_timeline(None)
    calls.clear()
    fake.post(DM, ALICE, text("live"))
    await loop.run_once()
    assert calls == []
    assert (await bodies(pool))[-1] == "live"
    assert len(await inbound_rows(pool)) == 7


async def test_backfill_respects_no_replay(fake, client, pool, monkeypatch):
    calls = spy_messages(monkeypatch, client)
    fake.limit_timeline(2)
    add_dm(fake)
    await rules.add_rule(pool, "allow", "room", DM, None)
    for n in range(4):
        fake.post(DM, ALICE, text(f"history {n}"))
    loop = make_loop(client, pool)
    await loop.run_once()
    assert await inbound_rows(pool) == []
    assert calls == []

    # Rooms joined while the bot was away: history before the bot's own join stays unread.
    for room_id in (LATE, FRESH):
        add_dm(fake, joined=False, room_id=room_id)
        await rules.add_rule(pool, "allow", "room", room_id, None)
        fake.post(room_id, ALICE, text(f"{room_id} before invite"))
        fake.invite(room_id, BRIDGE)
        fake.post(room_id, ALICE, text(f"{room_id} while invited"))
        fake.post(room_id, ALICE, text(f"{room_id} while invited again"))
        await client.join(room_id)
    for n in range(4):
        fake.post(LATE, ALICE, text(f"after join {n}"))
    fake.post(FRESH, ALICE, text("fresh after join"))
    await loop.run_once()
    assert sorted(await bodies(pool)) == sorted(
        ["fresh after join"] + [f"after join {n}" for n in range(4)]
    )
    late = await fetch(pool, "SELECT body FROM inbound WHERE room_id = %s ORDER BY id", LATE)
    assert [row[0] for row in late] == [f"after join {n}" for n in range(4)]
    # FRESH's limited timeline starts with the join itself, so there is nothing to backfill.
    assert [c[0] for c in calls] == [LATE]

    # A later profile change of the bot is not its join: backfill reads past it.
    calls.clear()
    fake.post(LATE, ALICE, text("before rename"))
    fake.set_member(LATE, BOT, "join", "hermes renamed")
    fake.post(LATE, ALICE, text("after rename 0"))
    fake.post(LATE, ALICE, text("after rename 1"))
    await loop.run_once()
    late = await fetch(pool, "SELECT body FROM inbound WHERE room_id = %s ORDER BY id", LATE)
    assert [row[0] for row in late][4:] == ["before rename", "after rename 0", "after rename 1"]
    assert [c[0] for c in calls] == [LATE]


async def test_backfill_cap(fake, client, pool, monkeypatch, caplog):
    assert make_settings().backfill_max_events == 500
    loop = await joined_and_synced(fake, client, pool, backfill_max_events=5)
    await rules.add_rule(pool, "allow", "room", DM, None)
    fake.post(DM, ALICE, text("recorded"))
    await loop.run_once()

    monkeypatch.setattr(sync_module, "BACKFILL_PAGE", 2)
    calls = spy_messages(monkeypatch, client)
    fake.limit_timeline(2)
    for n in range(10):
        fake.post(DM, ALICE, text(f"secret body {n}"))
    with caplog.at_level(logging.WARNING, logger="agents_matrix_connector.sync"):
        await loop.run_once()

    assert await bodies(pool) == ["recorded"] + [f"secret body {n}" for n in range(3, 10)]
    assert [c[2] for c in calls] == [2, 2, 1]
    assert sum(c[3] for c in calls) == 5
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert DM in warning.getMessage()
    assert " 5 " in warning.getMessage()
    assert "secret body" not in caplog.text
