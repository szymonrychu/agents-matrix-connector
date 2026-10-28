import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agents_matrix_connector.db import inbound, sends

ROOM = "!a:x"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


async def put(conn, session_id, *, kind="message", room=ROOM, text="hi", key=None, reply_to=None):
    return await sends.insert(
        conn,
        session_id=session_id,
        slug="hive-1",
        kind=kind,
        room_id=room,
        text=text if kind == "message" else None,
        key=key if kind == "reaction" else None,
        reply_to=reply_to,
        ref_event_id="$ref" if reply_to else None,
    )


async def status_of(conn, send_id):
    return (await sends.get(conn, send_id)).status


async def test_insert_and_get(conn):
    session = uuid.uuid4()
    send_id = await put(conn, session, reply_to="m7")
    row = await sends.get(conn, send_id)
    assert row == sends.SendRow(
        id=send_id,
        session_id=session,
        slug="hive-1",
        kind="message",
        room_id=ROOM,
        text="hi",
        key=None,
        reply_to="m7",
        ref_event_id="$ref",
        event_id=None,
        status="queued",
        reason=None,
        created_at=row.created_at,
        claimed_at=None,
        sent_at=None,
    )
    assert await sends.get(conn, send_id + 1000) is None
    with pytest.raises(ValueError):
        await put(conn, session, kind="other")


async def test_guarded_transitions(conn):
    session = uuid.uuid4()
    a, b, c = [await put(conn, session) for _ in range(3)]
    d = await put(conn, session, kind="reaction", key="+1")

    claimed = await sends.claim_queued(conn)
    assert [r.id for r in claimed] == [a, b, c, d]
    assert {r.status for r in claimed} == {"sending"}
    assert all(r.claimed_at is not None for r in claimed)
    assert await sends.claim_queued(conn) == []

    late = await put(conn, session)
    assert await sends.mark_sent(conn, late, "$early") is False
    assert await sends.mark_failed(conn, late, "x") is False
    assert await sends.mark_refused(conn, late, "x") is False
    assert await status_of(conn, late) == "queued"

    assert await sends.mark_sent(conn, a, "$a") is True
    assert await sends.mark_failed(conn, b, "homeserver") is True
    assert await sends.mark_refused(conn, c, "room_bound") is True
    for done in (a, b, c):
        assert await sends.mark_sent(conn, done, "$again") is False
        assert await sends.mark_failed(conn, done, "again") is False
        assert await sends.mark_refused(conn, done, "again") is False

    sent = await sends.get(conn, a)
    assert (sent.status, sent.event_id, sent.sent_at is not None) == ("sent", "$a", True)
    failed = await sends.get(conn, b)
    assert (failed.status, failed.reason, failed.event_id) == ("failed", "homeserver", None)
    refused = await sends.get(conn, c)
    assert (refused.status, refused.reason) == ("refused", "room_bound")
    assert await status_of(conn, d) == "sending"

    assert [r.id for r in await sends.claim_queued(conn)] == [late]
    assert await sends.claim_queued(conn) == []


async def test_rate_count_window(conn):
    session, other = uuid.uuid4(), uuid.uuid4()
    fresh = [
        await put(conn, session),
        await put(conn, session, kind="reaction", key="+1"),
        await put(conn, session),
        await put(conn, session),
    ]
    old = await put(conn, session)
    await put(conn, other)
    await sends.claim_queued(conn)
    await sends.mark_sent(conn, fresh[0], "$1")
    await sends.mark_failed(conn, fresh[2], "x")
    await sends.mark_refused(conn, fresh[3], "x")
    await conn.execute(
        "UPDATE sends SET created_at = now() - interval '61 seconds' WHERE id = %s", (old,)
    )
    await conn.execute(
        "UPDATE sends SET created_at = now() - interval '59 seconds' WHERE id = %s", (fresh[1],)
    )
    assert await sends.count_recent(conn, session, 60) == 4
    assert await sends.count_recent(conn, other, 60) == 1
    assert await sends.count_recent(conn, uuid.uuid4(), 60) == 0
    assert await sends.count_recent(conn, session, 3600) == 5


@pytest.mark.parametrize(
    ("ref", "parsed"),
    [
        ("m1", ("m", 1)),
        ("s42", ("s", 42)),
        ("m9223372036854775807", ("m", 9223372036854775807)),
        ("m9223372036854775808", None),
        ("m0", None),
        ("s01", None),
        ("x1", None),
        ("m", None),
        ("M1", None),
        ("m1 ", None),
        (" s1", None),
        ("m-1", None),
        ("s1.0", None),
        ("$event:x", None),
        ("", None),
        ("m١", None),
    ],
)
def test_parse_ref(ref, parsed):
    assert sends.parse_ref(ref) == parsed


async def test_resolve_references(conn):
    await inbound.insert(
        conn,
        event_id="$in",
        room_id=ROOM,
        sender="@u:x",
        sender_name="U",
        body="hello",
        mentioned=False,
        origin_ts=T0,
        status="new",
        reason=None,
    )
    cur = await conn.execute("SELECT id FROM inbound WHERE event_id = '$in'")
    inbound_id = (await cur.fetchone())[0]
    session = uuid.uuid4()
    sent_id = await put(conn, session, kind="reaction", key="+1", reply_to=f"m{inbound_id}")
    await sends.claim_queued(conn)
    await sends.mark_sent(conn, sent_id, "$reaction")
    queued_id = await put(conn, session, room="!b:x")

    assert await sends.resolve(conn, f"m{inbound_id}") == sends.Ref(
        source="inbound", id=inbound_id, room_id=ROOM, event_id="$in"
    )
    assert await sends.resolve(conn, f"s{sent_id}") == sends.Ref(
        source="send",
        id=sent_id,
        room_id=ROOM,
        event_id="$reaction",
        session_id=session,
        kind="reaction",
        status="sent",
    )
    assert await sends.resolve(conn, f"s{queued_id}") == sends.Ref(
        source="send",
        id=queued_id,
        room_id="!b:x",
        event_id=None,
        session_id=session,
        kind="message",
        status="queued",
    )
    for unknown in (f"m{inbound_id + 100}", f"s{queued_id + 100}", "m9223372036854775807"):
        assert await sends.resolve(conn, unknown) is None
    for malformed in ("$in", f"x{inbound_id}", "m9223372036854775808", "", "s"):
        assert await sends.resolve(conn, malformed) is None


async def test_prune_ended_sends(conn):
    session = uuid.uuid4()
    cutoff = datetime.now(UTC) - timedelta(days=1)
    old = datetime.now(UTC) - timedelta(days=3)
    sent_old, failed_old, refused_old, sending_old, sent_new = [
        await put(conn, session) for _ in range(5)
    ]
    await sends.claim_queued(conn)
    queued_old = await put(conn, session)
    await sends.mark_sent(conn, sent_old, "$1")
    await sends.mark_failed(conn, failed_old, "x")
    await sends.mark_refused(conn, refused_old, "x")
    await sends.mark_sent(conn, sent_new, "$2")
    await conn.execute(
        "UPDATE sends SET created_at = %s WHERE id = ANY(%s)",
        (old, [sent_old, failed_old, refused_old, sending_old, queued_old]),
    )

    assert await sends.prune(conn, cutoff) == 3
    cur = await conn.execute("SELECT id FROM sends ORDER BY id")
    assert [r[0] for r in await cur.fetchall()] == [sending_old, sent_new, queued_old]
    assert await sends.prune(conn, cutoff) == 0
