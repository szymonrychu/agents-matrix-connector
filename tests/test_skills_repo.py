import psycopg
import pytest

from agents_matrix_connector.db import skills

ROOM = "!a:x"


async def create(conn, name="deck-tune", room=ROOM, body="steps"):
    return await skills.put(conn, room, name, "when to use", body, 0, "hive-1")


async def test_skills_round_trip(conn):
    assert await skills.get(conn, ROOM, "deck-tune") is None
    row = await create(conn)
    assert (row.room_id, row.name, row.description, row.body, row.version) == (
        ROOM,
        "deck-tune",
        "when to use",
        "steps",
        1,
    )
    assert row.updated_by_slug == "hive-1"
    assert await skills.get(conn, ROOM, "deck-tune") == row


async def test_skill_update_and_stale(conn):
    first = await create(conn)
    second = await skills.put(conn, ROOM, "deck-tune", "new desc", "new body", 1, "hive-2")
    assert (second.version, second.description, second.body) == (2, "new desc", "new body")
    assert second.created_at == first.created_at
    assert await skills.put(conn, ROOM, "deck-tune", "d", "b", 1, None) is None
    assert await skills.put(conn, ROOM, "deck-tune", "d", "b", 0, None) is None
    assert await skills.put(conn, ROOM, "missing", "d", "b", 1, None) is None
    kept = await skills.get(conn, ROOM, "deck-tune")
    assert (kept.version, kept.body) == (2, "new body")


async def test_list_is_per_room_and_sorted(conn):
    await create(conn, "zeta")
    await create(conn, "alpha")
    await create(conn, "other", room="!b:x")
    assert [s.name for s in await skills.list_for_room(conn, ROOM)] == ["alpha", "zeta"]
    assert [s.name for s in await skills.list_for_room(conn, "!b:x")] == ["other"]
    assert await skills.list_for_room(conn, "!c:x") == []


async def test_count(conn):
    assert await skills.count(conn, ROOM) == 0
    await create(conn, "one")
    await create(conn, "two")
    await create(conn, "other", room="!b:x")
    assert await skills.count(conn, ROOM) == 2


async def test_delete_true_then_false(conn):
    await create(conn)
    assert await skills.delete(conn, ROOM, "deck-tune") is True
    assert await skills.delete(conn, ROOM, "deck-tune") is False
    assert await skills.get(conn, ROOM, "deck-tune") is None


async def test_db_rejects_bad_name(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        await create(conn, "Bad Name")


async def test_db_rejects_long_description(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        await skills.put(conn, ROOM, "ok", "x" * 1025, "b", 0, None)
    row = await skills.put(conn, ROOM, "ok", "x" * 1024, "b", 0, None)
    assert row is not None


async def test_lock_room_is_callable(conn):
    async with conn.transaction():
        await skills.lock_room(conn, ROOM)
