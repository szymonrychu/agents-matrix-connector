from agents_matrix_connector.db import notes

ROOM = "!a:x"


async def test_get_missing_is_none(conn):
    assert await notes.get(conn, ROOM) is None


async def test_put_creates_with_version_zero(conn):
    row = await notes.put(conn, ROOM, "## Requests\n- x", 0, "hive-1")
    assert (row.room_id, row.body, row.version, row.updated_by_slug) == (
        ROOM,
        "## Requests\n- x",
        1,
        "hive-1",
    )
    assert await notes.get(conn, ROOM) == row


async def test_put_updates_matching_version(conn):
    await notes.put(conn, ROOM, "one", 0, "hive-1")
    row = await notes.put(conn, ROOM, "two", 1, "hive-2")
    assert (row.body, row.version, row.updated_by_slug) == ("two", 2, "hive-2")


async def test_put_stale_version_returns_none_and_keeps_body(conn):
    await notes.put(conn, ROOM, "one", 0, None)
    await notes.put(conn, ROOM, "two", 1, None)
    assert await notes.put(conn, ROOM, "three", 1, None) is None
    row = await notes.get(conn, ROOM)
    assert (row.body, row.version) == ("two", 2)


async def test_put_zero_on_existing_returns_none(conn):
    await notes.put(conn, ROOM, "one", 0, None)
    assert await notes.put(conn, ROOM, "other", 0, None) is None
    assert (await notes.get(conn, ROOM)).body == "one"


async def test_rooms_are_separate_rows(conn):
    await notes.put(conn, ROOM, "a", 0, None)
    await notes.put(conn, "!b:x", "b", 0, None)
    assert (await notes.get(conn, ROOM)).body == "a"
    assert (await notes.get(conn, "!b:x")).body == "b"
