from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from agents_common.db.pool import open_pool

from agents_matrix_connector.db import holds, inbound
from agents_matrix_connector.discovery import Media

PACKAGE = "agents_matrix_connector"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


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


async def put(conn, event_id, room="!a:x", status="new", mentioned=False, reason=None):
    return await inbound.insert(
        conn,
        event_id=event_id,
        room_id=room,
        sender="@u:x",
        sender_name="U",
        body=f"body {event_id}",
        mentioned=mentioned,
        origin_ts=T0,
        status=status,
        reason=reason,
    )


async def new_run(conn, room="!a:x", status="in-progress") -> int:
    cur = await conn.execute(
        "INSERT INTO runs (room_id, preset_id, kind, status)"
        " VALUES (%s, gen_random_uuid(), 'first', %s) RETURNING id",
        (room, status),
    )
    return (await cur.fetchone())[0]


async def statuses(conn) -> dict[str, str]:
    cur = await conn.execute("SELECT event_id, status FROM inbound")
    return dict(await cur.fetchall())


async def test_insert_idempotent(conn):
    assert await put(conn, "$1", mentioned=True) is True
    assert await put(conn, "$1", status="ignored", reason="dup") is False
    cur = await conn.execute("SELECT count(*), min(status), bool_and(mentioned) FROM inbound")
    assert await cur.fetchone() == (1, "new", True)
    assert await inbound.count_new(conn, "!a:x") == 1
    assert await put(conn, "$2", status="ignored", reason="denied") is True
    assert await inbound.count_new(conn, "!a:x") == 1


async def test_rooms_ready(conn):
    await put(conn, "$b1", room="!b:x")
    await put(conn, "$a1", room="!a:x")
    await put(conn, "$c1", room="!c:x")
    await put(conn, "$b2", room="!b:x")
    await put(conn, "$d1", room="!d:x", status="ignored", reason="x")
    assert await inbound.rooms_ready(conn, T0) == ["!b:x", "!a:x", "!c:x"]
    await new_run(conn, "!a:x")
    await new_run(conn, "!c:x", status="handled")
    assert await inbound.rooms_ready(conn, T0) == ["!b:x", "!c:x"]


async def test_rooms_ready_skips_held_rooms(conn):
    await put(conn, "$a1", room="!a:x")
    await put(conn, "$b1", room="!b:x")
    await holds.hold(conn, "!a:x", T0 + timedelta(minutes=10), "usage_quota")
    assert await inbound.rooms_ready(conn, T0) == ["!b:x"]
    assert await inbound.rooms_ready(conn, T0 + timedelta(minutes=9)) == ["!b:x"]
    # A hold ends at its `until`, and a hold on a room without new rows changes nothing.
    assert await inbound.rooms_ready(conn, T0 + timedelta(minutes=10)) == ["!a:x", "!b:x"]
    await holds.hold(conn, "!z:x", T0 + timedelta(days=1), "usage_quota")
    assert await inbound.rooms_ready(conn, T0 + timedelta(minutes=11)) == ["!a:x", "!b:x"]


async def test_hold_upserts_and_moves_until(conn):
    await holds.hold(conn, "!a:x", T0 + timedelta(minutes=10), "usage_quota")
    await holds.hold(conn, "!a:x", T0 + timedelta(hours=2), "usage_quota")
    cur = await conn.execute("SELECT room_id, until, reason FROM room_holds")
    assert await cur.fetchall() == [("!a:x", T0 + timedelta(hours=2), "usage_quota")]


async def test_claim_room(conn):
    await put(conn, "$1")
    await put(conn, "$o", room="!o:x")
    await put(conn, "$2")
    await put(conn, "$ig", status="ignored", reason="x")
    run = await new_run(conn)
    rows = await inbound.claim_room(conn, "!a:x", run)
    assert [r.event_id for r in rows] == ["$1", "$2"]
    assert all(r.status == "in-progress" and r.run_id == run for r in rows)
    assert rows[0].body == "body $1" and rows[0].origin_ts == T0
    assert await statuses(conn) == {
        "$1": "in-progress",
        "$2": "in-progress",
        "$o": "new",
        "$ig": "ignored",
    }
    assert await inbound.claim_room(conn, "!a:x", run) == []


async def test_run_rows(conn):
    await put(conn, "$1")
    await put(conn, "$o", room="!o:x")
    await put(conn, "$2")
    run, other = await new_run(conn), await new_run(conn, "!o:x")
    await inbound.claim_room(conn, "!a:x", run)
    await inbound.claim_room(conn, "!o:x", other)
    await put(conn, "$3")
    rows = await inbound.run_rows(conn, run)
    assert [r.event_id for r in rows] == ["$1", "$2"]
    assert all(r.status == "in-progress" and r.run_id == run for r in rows)
    assert rows[0].body == "body $1" and rows[0].origin_ts == T0
    await inbound.finish_run_rows(conn, other, "handled")
    assert await inbound.run_rows(conn, other) == []


async def test_guarded_transitions(conn):
    await put(conn, "$1")
    await put(conn, "$2")
    await put(conn, "$o", room="!o:x")
    run, other = await new_run(conn), await new_run(conn, "!o:x")
    await inbound.claim_room(conn, "!a:x", run)
    await inbound.claim_room(conn, "!o:x", other)
    assert await inbound.ignore_new(conn, "!a:x", "paused") == 0
    assert await inbound.finish_run_rows(conn, run + 100, "handled") == 0
    assert await inbound.requeue_run_rows(conn, run) == 2
    cur = await conn.execute("SELECT count(*) FROM inbound WHERE run_id IS NOT NULL")
    assert (await cur.fetchone())[0] == 1
    assert await inbound.finish_run_rows(conn, run, "handled") == 0
    assert await inbound.ignore_new(conn, "!a:x", "paused") == 2
    assert await inbound.ignore_new(conn, "!a:x", "paused") == 0
    assert await inbound.finish_run_rows(conn, other, "failed", "boom") == 1
    assert await inbound.finish_run_rows(conn, other, "handled") == 0
    assert await inbound.requeue_run_rows(conn, other) == 0
    cur = await conn.execute("SELECT status, reason FROM inbound WHERE event_id = '$o'")
    assert await cur.fetchone() == ("failed", "boom")
    cur = await conn.execute("SELECT DISTINCT reason FROM inbound WHERE room_id = '!a:x'")
    assert await cur.fetchall() == [("paused",)]
    with pytest.raises(ValueError):
        await inbound.finish_run_rows(conn, run, "new")


async def test_requeue_keeps_fifo(conn):
    await put(conn, "$1")
    run = await new_run(conn)
    await inbound.claim_room(conn, "!a:x", run)
    await put(conn, "$2")
    await inbound.requeue_run_rows(conn, run)
    rows = await inbound.claim_room(conn, "!a:x", run)
    assert [r.event_id for r in rows] == ["$1", "$2"]


async def test_list_and_prune(conn, pool):
    for i in range(1, 6):
        await put(conn, f"$a{i}")
    await put(conn, "$b1", room="!b:x", status="ignored", reason="x")
    run = await new_run(conn)
    await inbound.claim_room(conn, "!a:x", run)
    await inbound.finish_run_rows(conn, run, "handled")
    allrows = await inbound.list_messages(pool)
    assert [r.event_id for r in allrows] == ["$b1", "$a5", "$a4", "$a3", "$a2", "$a1"]
    assert [r.event_id for r in await inbound.list_messages(pool, room_id="!b:x")] == ["$b1"]
    assert [r.event_id for r in await inbound.list_messages(pool, status="ignored")] == ["$b1"]
    page = await inbound.list_messages(pool, room_id="!a:x", limit=2, before_id=allrows[1].id)
    assert [r.event_id for r in page] == ["$a4", "$a3"]

    await put(conn, "$new")
    await put(conn, "$busy", room="!c:x")
    await inbound.claim_room(conn, "!c:x", await new_run(conn, "!c:x"))
    await conn.execute("UPDATE inbound SET received_at = %s", (T0 - timedelta(days=10),))
    await conn.execute(
        "UPDATE inbound SET received_at = %s WHERE event_id = '$a5'", (T0 + timedelta(days=1),)
    )
    assert await inbound.prune(conn, T0) == 5
    left = await statuses(conn)
    assert left == {"$a5": "handled", "$new": "new", "$busy": "in-progress"}


async def deny(conn, event_id, room, sender, name, reason, at):
    await conn.execute(
        "INSERT INTO inbound (event_id, room_id, sender, sender_name, body, status, reason,"
        " received_at) VALUES (%s, %s, %s, %s, NULL, 'ignored', %s, %s)",
        (event_id, room, sender, name, reason, at),
    )


async def test_denied_summary_groups_by_room(conn):
    await deny(conn, "$1", "!a:x", "@s1:x", "S1", "not_allowed", T0)
    await deny(conn, "$2", "!a:x", "@s2:x", "S2", "denied: user", T0 + timedelta(minutes=1))
    await deny(conn, "$3", "!a:x", "@s1:x", "S1 new", "not_allowed", T0 + timedelta(minutes=2))
    await deny(conn, "$4", "!b:x", "@s3:x", None, "not_allowed", T0)
    await deny(conn, "$5", "!b:x", "@s4:x", "S4", "not_mentioned", T0 + timedelta(hours=1))
    await put(conn, "$6", room="!c:x", status="new")
    summary = await inbound.denied_summary(conn)
    assert set(summary) == {"!a:x", "!b:x"}
    a = summary["!a:x"]
    assert a.count == 3
    assert a.last_at == T0 + timedelta(minutes=2)
    assert a.senders == [("@s1:x", "S1 new"), ("@s2:x", "S2")]
    b = summary["!b:x"]
    assert (b.count, b.last_at, b.senders) == (1, T0, [("@s3:x", None)])
    only = await inbound.denied_summary(conn, ["!b:x", "!c:x"])
    assert set(only) == {"!b:x"}


PHOTO = Media("image", "mxc://x/p", "image/jpeg", 1234, "IMG_1.jpg", 30, 20)


async def put_media(
    conn,
    event_id,
    at,
    sender="@u:x",
    status="ignored",
    reason="not_mentioned",
    media=PHOTO,
    mentioned=False,
    room="!a:x",
):
    return await inbound.insert(
        conn,
        event_id=event_id,
        room_id=room,
        sender=sender,
        sender_name="U",
        body="",
        mentioned=mentioned,
        origin_ts=at,
        status=status,
        reason=reason,
        media=media,
    )


async def test_insert_media_round_trip(conn, pool):
    await put_media(conn, "$m", T0, status="new", reason=None)
    [row] = await inbound.list_messages(pool)
    assert (row.media_kind, row.media_url, row.media_mime, row.media_size) == (
        "image",
        "mxc://x/p",
        "image/jpeg",
        1234,
    )
    assert (row.media_name, row.media_w, row.media_h) == ("IMG_1.jpg", 30, 20)
    assert (row.media_status, row.media_reason) == (None, None)
    await put(conn, "$t")
    plain = [r for r in await inbound.list_messages(pool) if r.event_id == "$t"][0]
    assert plain.media_kind is None and plain.media_url is None


async def test_set_media_outcome_guarded(conn, pool):
    await put_media(conn, "$m", T0, status="new", reason=None)
    run_id = await new_run(conn)
    other = await new_run(conn, "!b:x")
    [row] = await inbound.claim_room(conn, "!a:x", run_id)
    assert await inbound.set_media_outcome(conn, row.id, other, "attached", None) is False
    assert await inbound.set_media_outcome(conn, row.id, run_id, "not_attached", "too_large")
    [stored] = await inbound.list_messages(pool)
    assert (stored.media_status, stored.media_reason) == ("not_attached", "too_large")
    await inbound.finish_run_rows(conn, run_id, "handled")
    assert await inbound.set_media_outcome(conn, row.id, run_id, "attached", None) is False
    with pytest.raises(ValueError):
        await inbound.set_media_outcome(conn, row.id, run_id, "bogus", None)


async def test_promote_media_before_mention(conn):
    mention = T0 + timedelta(minutes=10)
    await put_media(conn, "$30", mention - timedelta(seconds=30))
    await put_media(conn, "$100", mention - timedelta(seconds=100))
    await put_media(conn, "$130", mention - timedelta(seconds=130))
    await put_media(conn, "$text", mention - timedelta(seconds=10), media=None)
    await put_media(conn, "$denied", mention - timedelta(seconds=10), reason="not_allowed")
    assert await inbound.promote_media(conn, "!a:x", "@u:x", mention, 120, 10) == 2
    got = await statuses(conn)
    assert (got["$30"], got["$100"], got["$130"]) == ("new", "new", "ignored")
    assert (got["$text"], got["$denied"]) == ("ignored", "ignored")
    cur = await conn.execute("SELECT reason FROM inbound WHERE event_id = '$30'")
    assert await cur.fetchone() == (None,)


async def test_promote_media_other_sender_untouched(conn):
    await put_media(conn, "$mine", T0, sender="@u:x")
    await put_media(conn, "$theirs", T0, sender="@v:x")
    await put_media(conn, "$other_room", T0, room="!b:x")
    assert await inbound.promote_media(conn, "!a:x", "@u:x", T0, 120, 10) == 1
    got = await statuses(conn)
    assert (got["$mine"], got["$theirs"], got["$other_room"]) == ("new", "ignored", "ignored")


async def test_promote_media_respects_queue_cap(conn):
    mention = T0 + timedelta(minutes=10)
    for seconds in (10, 20, 30):
        await put_media(conn, f"${seconds}", mention - timedelta(seconds=seconds))
    assert await inbound.promote_media(conn, "!a:x", "@u:x", mention, 120, 2) == 2
    got = await statuses(conn)
    assert (got["$10"], got["$20"], got["$30"]) == ("new", "new", "ignored")
    assert await inbound.promote_media(conn, "!a:x", "@u:x", mention, 120, 0) == 0


async def test_recent_accepted_mention(conn):
    mention = T0
    await put_media(conn, "$m", mention, status="new", reason=None, media=None, mentioned=True)
    later = mention + timedelta(seconds=60)
    assert await inbound.recent_accepted_mention(conn, "!a:x", "@u:x", later, 120) is True
    assert await inbound.recent_accepted_mention(conn, "!a:x", "@v:x", later, 120) is False
    assert await inbound.recent_accepted_mention(conn, "!b:x", "@u:x", later, 120) is False
    too_late = mention + timedelta(seconds=121)
    assert await inbound.recent_accepted_mention(conn, "!a:x", "@u:x", too_late, 120) is False
    await conn.execute("UPDATE inbound SET status = 'handled'")
    assert await inbound.recent_accepted_mention(conn, "!a:x", "@u:x", later, 120) is True
    await conn.execute("UPDATE inbound SET status = 'ignored', reason = 'not_allowed'")
    assert await inbound.recent_accepted_mention(conn, "!a:x", "@u:x", later, 120) is False
