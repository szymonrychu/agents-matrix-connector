import re
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from agents_common.db.pool import open_pool

from agents_matrix_connector.db import directory
from agents_matrix_connector.discovery import RoomInfo, facebook_id

PACKAGE = "agents_matrix_connector"
FB = re.compile(r"^@facebook_(?P<id>\d+):")
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def fb(user_id: str) -> str | None:
    return facebook_id(user_id, FB, "example.org")


def info(**kw) -> RoomInfo:
    base = dict(
        kind="dm",
        name="Alice",
        alias=None,
        encrypted=False,
        message_request=False,
        members={},
        member_count=2,
    )
    base.update(kw)
    return RoomInfo(**base)


@pytest.fixture
def url(migrated_db):
    return migrated_db(PACKAGE)


@pytest.fixture
async def conn(url):
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
        yield c


@pytest.fixture
async def pool(url):
    p = await open_pool(url)
    yield p
    await p.close()


async def _one(conn, query, *args):
    cur = await conn.execute(query, args)
    return await cur.fetchone()


async def test_upsert_room_idempotent(conn):
    await directory.upsert_room(conn, "!a:x", membership="invite", inviter="@bob:x", at=T0)
    await directory.upsert_room(
        conn,
        "!a:x",
        membership="join",
        info=info(kind="group", name="Team", alias="#t:x", encrypted=True, message_request=True),
        at=T0 + timedelta(hours=1),
    )
    assert (await _one(conn, "SELECT count(*) FROM rooms"))[0] == 1
    row = await _one(
        conn,
        "SELECT membership, inviter, kind, name, alias, encrypted, message_request,"
        " discovered_at, updated_at FROM rooms",
    )
    assert row[:7] == ("join", "@bob:x", "group", "Team", "#t:x", True, True)
    assert row[7] == T0
    assert row[8] == T0 + timedelta(hours=1)


async def test_upsert_members(conn):
    await directory.upsert_room(conn, "!a:x", membership="join", at=T0)
    alice = "@facebook_111:example.org"
    bot = "@bot:example.org"
    await directory.upsert_members(
        conn,
        "!a:x",
        {alice: ("Alice", "join"), bot: (None, "join"), "@gone:x": ("Gone", "leave")},
        fb,
        T0,
    )
    assert (await _one(conn, "SELECT member_count FROM rooms"))[0] == 2
    assert (
        await _one(conn, "SELECT facebook_id, display_name FROM users WHERE user_id=%s", alice)
    ) == (
        "111",
        "Alice",
    )
    assert (await _one(conn, "SELECT facebook_id FROM users WHERE user_id=%s", bot))[0] is None
    await directory.upsert_members(
        conn,
        "!a:x",
        {alice: ("Alice B", "join"), "@gone:x": ("Gone", "leave")},
        fb,
        T0 + timedelta(1),
    )
    assert (
        await _one(conn, "SELECT display_name, last_seen_at FROM users WHERE user_id=%s", alice)
    ) == (
        "Alice B",
        T0 + timedelta(1),
    )
    assert (await _one(conn, "SELECT display_name FROM room_members WHERE user_id=%s", alice))[
        0
    ] == "Alice B"
    assert (await _one(conn, "SELECT member_count FROM rooms"))[0] == 2
    assert (await _one(conn, "SELECT count(*) FROM room_members"))[0] == 3
    await directory.set_membership(conn, "!a:x", "leave", T0)
    assert (await _one(conn, "SELECT membership FROM rooms"))[0] == "leave"


async def test_sync_state_round_trip(conn, pool):
    assert await directory.get_sync_state(conn) == (None, None)
    assert await directory.last_sync_at(pool) is None
    await directory.set_sync_state(conn, next_batch="s1", filter_id="f1", at=T0)
    assert await directory.get_sync_state(conn) == ("s1", "f1")
    await directory.set_sync_state(conn, next_batch="s2", at=T0 + timedelta(1))
    assert await directory.get_sync_state(conn) == ("s2", "f1")
    assert await directory.last_sync_at(pool) == T0 + timedelta(1)


async def test_list_rooms_counts_and_session(conn, pool):
    for rid in ("!old:x", "!new:x", "!none:x"):
        await directory.upsert_room(conn, rid, membership="join", info=info(kind="group"), at=T0)
    await directory.touch_room(conn, "!old:x", T0)
    await directory.touch_room(conn, "!new:x", T0 + timedelta(1))
    for i, status in enumerate(["new", "new", "handled"]):
        await conn.execute(
            "INSERT INTO inbound (event_id, room_id, sender, status)"
            " VALUES (%s, '!new:x', '@s:x', %s)",
            (f"$e{i}", status),
        )
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    for sid, slug, status in ((s1, "one", "handled"), (s2, "two", "in-progress")):
        await conn.execute(
            "INSERT INTO runs (room_id, preset_id, kind, slug, session_id, status)"
            " VALUES ('!new:x', %s, 'first', %s, %s, %s)",
            (uuid.uuid4(), slug, sid, status),
        )
    rooms = await directory.list_rooms(pool)
    assert [r.room_id for r in rooms] == ["!new:x", "!old:x", "!none:x"]
    new = rooms[0]
    assert new.queued == 2
    assert new.session == directory.SessionRef(session_id=s2, slug="two", status="in-progress")
    assert rooms[1].queued == 0 and rooms[1].session is None
    assert [r.room_id for r in await directory.list_rooms(pool, kind="dm")] == []
    assert len(await directory.list_rooms(pool, kind="group")) == 3
    assert await directory.room_exists(pool, "!new:x") is True
    assert await directory.room_exists(pool, "!nope:x") is False


async def test_get_room_and_list_users(conn, pool):
    await directory.upsert_room(conn, "!a:x", membership="join", info=info(), at=T0)
    await directory.upsert_room(conn, "!b:x", membership="join", info=info(), at=T0)
    alice = "@facebook_111:example.org"
    carol = "@facebook_222:example.org"
    await directory.upsert_members(
        conn, "!a:x", {alice: ("Alice", "join"), carol: ("Carol", "leave")}, fb, T0
    )
    await directory.upsert_members(conn, "!b:x", {alice: ("Alice", "join")}, fb, T0)
    room = await directory.get_room(pool, "!a:x")
    assert room is not None and room.name == "Alice" and room.member_count == 1
    assert {m.user_id: (m.display_name, m.facebook_id, m.membership) for m in room.members} == {
        alice: ("Alice", "111", "join"),
        carol: ("Carol", "222", "leave"),
    }
    assert await directory.get_room(pool, "!nope:x") is None
    users = await directory.list_users(pool, None, 10)
    assert [u.user_id for u in users] == [alice, carol]
    assert users[0].room_ids == ["!a:x", "!b:x"] and users[1].room_ids == []
    assert [u.user_id for u in await directory.list_users(pool, "ALICE", 10)] == [alice]
    assert [u.user_id for u in await directory.list_users(pool, "222", 10)] == [carol]
    assert [u.user_id for u in await directory.list_users(pool, "facebook_111", 10)] == [alice]
    assert len(await directory.list_users(pool, None, 1)) == 1
    assert await directory.list_users(pool, "zzz", 10) == []
