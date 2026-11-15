"""The history crawl queue: one crawl per bound room, ever; status-guarded transitions."""

from datetime import UTC, datetime, timedelta

import pytest

from agents_matrix_connector.db import history_crawls

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


async def bind(conn, room_id: str, *, released: bool = False) -> None:
    await conn.execute(
        "INSERT INTO room_bindings (room_id, origin, run_id, released_at) VALUES (%s, 'inbound', 1,"
        " %s)",
        (room_id, T0 if released else None),
    )


async def rows(conn) -> list[tuple]:
    cur = await conn.execute(
        "SELECT room_id, status, reason, token, events, inserted FROM history_crawls ORDER BY id"
    )
    return await cur.fetchall()


async def test_enqueue_bound_once_per_room(conn):
    await bind(conn, "!a:hs")
    await bind(conn, "!gone:hs", released=True)
    assert await history_crawls.enqueue_bound(conn) == 1
    assert await history_crawls.enqueue_bound(conn) == 0
    assert await rows(conn) == [("!a:hs", "queued", None, None, 0, 0)]

    crawl = await history_crawls.next_ready(conn, T0)
    assert await history_crawls.start(conn, crawl.id, "t1")
    assert await history_crawls.finish(conn, crawl.id, "done", "complete")
    await conn.execute("UPDATE room_bindings SET released_at = now()")
    await bind(conn, "!a:hs")
    assert await history_crawls.enqueue_bound(conn) == 0
    assert await rows(conn) == [("!a:hs", "done", "complete", "t1", 0, 0)]


async def test_next_ready_respects_not_before(conn):
    await bind(conn, "!a:hs")
    await bind(conn, "!b:hs")
    await history_crawls.enqueue_bound(conn)
    first = await history_crawls.next_ready(conn, T0)
    assert first.room_id == "!a:hs" and first.status == "queued"
    assert await history_crawls.defer(conn, first.id, "queued", T0 + timedelta(seconds=60))
    second = await history_crawls.next_ready(conn, T0)
    assert second.room_id == "!b:hs"
    assert await history_crawls.finish(conn, second.id, "failed", "matrix_error")
    assert await history_crawls.next_ready(conn, T0 + timedelta(seconds=59)) is None
    again = await history_crawls.next_ready(conn, T0 + timedelta(seconds=60))
    assert again.id == first.id
    assert again.not_before == T0 + timedelta(seconds=60)

    assert await history_crawls.start(conn, first.id, "t9")
    crawling = await history_crawls.next_ready(conn, T0 + timedelta(seconds=60))
    assert crawling.status == "crawling" and crawling.token == "t9"
    assert await history_crawls.defer(conn, first.id, "crawling", T0 + timedelta(seconds=120))
    assert await history_crawls.progress(conn, first.id, token="t5", events=3, inserted=2)
    progressed = await history_crawls.next_ready(conn, T0)
    assert (progressed.token, progressed.events, progressed.inserted) == ("t5", 3, 2)
    assert progressed.not_before is None


async def test_transitions_are_status_guarded(conn):
    await bind(conn, "!a:hs")
    await history_crawls.enqueue_bound(conn)
    crawl = await history_crawls.next_ready(conn, T0)
    assert not await history_crawls.progress(conn, crawl.id, token="t1", events=1, inserted=1)
    assert not await history_crawls.defer(conn, crawl.id, "crawling", T0)
    assert await history_crawls.start(conn, crawl.id, "t1")
    assert not await history_crawls.start(conn, crawl.id, "t2")
    assert await history_crawls.finish(conn, crawl.id, "done", "cap")
    assert not await history_crawls.finish(conn, crawl.id, "failed", "matrix_error")
    assert not await history_crawls.progress(conn, crawl.id, token="t3", events=1, inserted=1)
    with pytest.raises(ValueError):
        await history_crawls.defer(conn, crawl.id, "done", T0)
    assert await history_crawls.next_ready(conn, T0) is None
    done = await conn.execute("SELECT status, reason, done_at IS NOT NULL FROM history_crawls")
    assert await done.fetchall() == [("done", "cap", True)]

    await bind(conn, "!b:hs")
    await history_crawls.enqueue_bound(conn)
    queued = await history_crawls.next_ready(conn, T0)
    assert await history_crawls.finish(conn, queued.id, "done", "not_visible")
    for bad in ("queued", "crawling"):
        with pytest.raises(ValueError):
            await history_crawls.finish(conn, queued.id, bad, "x")
