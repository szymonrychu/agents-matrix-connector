import pytest

from agents_matrix_connector.db import notes

ROOM = "!a:x"
CAPS = {"section_cap": 1000, "total_cap": 1000}


async def put(conn, section, body, version, slug=None, room=ROOM):
    async with conn.transaction():
        return await notes.put_section(conn, room, section, body, version, slug, **CAPS)


async def test_get_all_missing_is_empty(conn):
    assert await notes.get_all(conn, ROOM) == []


async def test_put_section_creates_then_updates(conn):
    row = await put(conn, "requests", "- x", 0, "hive-1")
    assert (row.room_id, row.section, row.body, row.version, row.updated_by_slug) == (
        ROOM,
        "requests",
        "- x",
        1,
        "hive-1",
    )
    row = await put(conn, "requests", "- y", 1, "hive-2")
    assert (row.body, row.version, row.updated_by_slug) == ("- y", 2, "hive-2")
    assert await notes.get_all(conn, ROOM) == [row]


@pytest.mark.parametrize(("expected", "current"), [(0, 1), (2, 1)])
async def test_put_section_conflict_names_current_version(conn, expected, current):
    await put(conn, "requests", "one", 0)
    with pytest.raises(notes.VersionConflict) as exc:
        await put(conn, "requests", "other", expected)
    assert (exc.value.expected, exc.value.current) == (expected, current)
    [row] = await notes.get_all(conn, ROOM)
    assert (row.body, row.version) == ("one", 1)


async def test_rooms_are_separate(conn):
    await put(conn, "requests", "a", 0)
    await put(conn, "requests", "b", 0, room="!b:x")
    assert [r.body for r in await notes.get_all(conn, ROOM)] == ["a"]
    assert [r.body for r in await notes.get_all(conn, "!b:x")] == ["b"]
