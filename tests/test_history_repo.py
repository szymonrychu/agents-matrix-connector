import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from history_helpers import ALICE, BOB, add_m, add_o, add_s

from agents_matrix_connector.api.history import _WITHHELD
from agents_matrix_connector.db import history

R = "!r:x"
OTHER = "!other:x"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def t(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def _page(conn, **kw):
    args = {
        "before": None,
        "after": None,
        "sender_name": None,
        "sender_digest": None,
        "limit": 50,
        "withheld_reasons": list(_WITHHELD),
    }
    args.update(kw)
    return await history.page(conn, R, **args)


def _ids(rows):
    return [(r.kind, r.id) for r in rows]


@pytest.mark.parametrize(
    ("ref", "parsed"),
    [
        ("m1", ("m", 1)),
        ("s42", ("s", 42)),
        ("o7", ("o", 7)),
        ("m0", None),
        ("x1", None),
        ("m01", None),
        ("m", None),
        ("M1", None),
        (" m1", None),
        ("m1 ", None),
        ("m\u0661", None),
        (f"m{2**63 - 1}", ("m", 2**63 - 1)),
        (f"m{2**63}", None),
    ],
)
def test_parse_ref(ref, parsed):
    assert history.parse_ref(ref) == parsed


async def test_page_merges_three_sources_in_time_order(conn):
    m1 = await add_m(conn, R, t(1))
    o1 = await add_o(conn, R, t(2))
    s1 = await add_s(conn, R, t(3), event_id="$s1")
    await add_s(conn, R, t(4), kind="reaction")
    await add_o(conn, R, t(5), kind="agent")
    await add_m(conn, OTHER, t(6))
    rows, more = await _page(conn)
    assert _ids(rows) == [("m", m1), ("o", o1), ("s", s1)]
    assert more is False
    assert [r.at for r in rows] == [t(1), t(2), t(3)]


async def test_page_m_at_falls_back_to_received_at(conn):
    cur = await conn.execute(
        "INSERT INTO inbound (event_id, room_id, sender, status, received_at)"
        " VALUES ('$noorigin', %s, %s, 'handled', %s) RETURNING id",
        (R, ALICE, t(10)),
    )
    m_id = (await cur.fetchone())[0]
    rows, _ = await _page(conn)
    assert rows[0].id == m_id and rows[0].at == t(10)


async def test_page_walk_visits_every_item_once_with_ties(conn):
    same = t(5)
    expected = []
    expected += [("m", await add_m(conn, R, same)) for _ in range(3)]
    expected += [("s", await add_s(conn, R, same)) for _ in range(2)]
    expected += [("o", await add_o(conn, R, same)) for _ in range(2)]
    expected += [("m", await add_m(conn, R, t(1))), ("o", await add_o(conn, R, t(9)))]
    expected.append(("s", await add_s(conn, R, t(12))))
    assert len(expected) == 10

    backward = []
    rows, more = await _page(conn, limit=1)
    while True:
        assert len(rows) == 1
        backward.append(rows[0])
        if not more:
            break
        cursor = (rows[0].at, rows[0].kind, rows[0].id)
        rows, more = await _page(conn, before=cursor, limit=1)

    forward = []
    first = backward[-1]
    forward.append(first)
    cursor = (first.at, first.kind, first.id)
    while True:
        rows, more = await _page(conn, after=cursor, limit=1)
        if not rows:
            break
        forward.append(rows[0])
        cursor = (rows[0].at, rows[0].kind, rows[0].id)
        if not more:
            break

    assert len(set(_ids(backward))) == 10 and set(_ids(backward)) == set(expected)
    assert _ids(forward) == list(reversed(_ids(backward)))
    keys = [(r.at, r.kind, r.id) for r in forward]
    assert keys == sorted(keys)


async def test_page_before_returns_ascending_rows(conn):
    ids = [await add_m(conn, R, t(i)) for i in range(5)]
    last = await history.position(conn, R, f"m{ids[4]}")
    rows, more = await _page(conn, before=last, limit=2)
    assert _ids(rows) == [("m", ids[2]), ("m", ids[3])]
    assert more is True


async def test_position_rejects_foreign_unknown_and_wrong_kind(conn):
    mine = await add_m(conn, R, t(1))
    foreign = await add_m(conn, OTHER, t(1))
    send = await add_s(conn, R, t(2), event_id="$sent")
    reaction = await add_s(conn, R, t(3), kind="reaction", event_id="$react")
    reply = await add_o(conn, R, t(4))
    notice = await add_o(conn, R, t(5), kind="notice")
    (with_send,) = await (
        await conn.execute("SELECT id FROM outbound WHERE send_id = %s", (send,))
    ).fetchone()
    agent_o = await add_o(conn, R, t(6), kind="agent")
    assert await history.position(conn, R, f"m{mine}") == (t(1), "m", mine)
    assert await history.position(conn, R, f"s{send}") == (t(2), "s", send)
    assert await history.position(conn, R, f"o{reply}") == (t(4), "o", reply)
    assert await history.position(conn, R, f"o{notice}") == (t(5), "o", notice)
    for ref in [
        f"m{foreign}",
        "m999999",
        "s99",
        f"s{reaction}",
        f"o{with_send}",
        f"o{agent_o}",
        f"s{mine + 1000}",
        "x1",
        "m0",
        "",
        f"m{2**63}",
    ]:
        assert await history.position(conn, R, ref) is None, ref
    assert await history.position(conn, OTHER, f"m{mine}") is None


async def test_reply_to_resolution(conn):
    target_m = await add_m(conn, R, t(1), event_id="$target-m")
    target_s = await add_s(conn, R, t(2), event_id="$target-s")
    target_o = await add_o(conn, R, t(3), event_id="$target-o")
    await add_m(conn, OTHER, t(3), event_id="$foreign")
    r_m = await add_m(conn, R, t(4), reply_to="$target-m")
    r_s = await add_m(conn, R, t(5), reply_to="$target-s")
    r_o = await add_m(conn, R, t(6), reply_to="$target-o")
    r_unknown = await add_m(conn, R, t(7), reply_to="$gone")
    r_foreign = await add_m(conn, R, t(8), reply_to="$foreign")
    plain = await add_m(conn, R, t(9))
    s_reply = await add_s(conn, R, t(10), reply_to=f"m{target_m}")
    o_reply = await add_o(conn, R, t(11), reply_to_event="$target-s")
    o_unknown = await add_o(conn, R, t(12), reply_to_event="$nope")
    rows, _ = await _page(conn)
    got = {(r.kind, r.id): r.reply_to for r in rows}
    assert got[("m", r_m)] == f"m{target_m}"
    assert got[("m", r_s)] == f"s{target_s}"
    assert got[("m", r_o)] == f"o{target_o}"
    assert got[("m", r_unknown)] == "unknown"
    assert got[("m", r_foreign)] == "unknown"
    assert got[("m", plain)] is None
    assert got[("s", s_reply)] == f"m{target_m}"
    assert got[("o", o_reply)] == f"s{target_s}"
    assert got[("o", o_unknown)] == "unknown"
    assert got[("o", target_o)] is None


async def test_sender_filters(conn):
    a1 = await add_m(conn, R, t(1), name="Alice")
    a2 = await add_m(conn, R, t(2), name="ALICE")
    await add_m(conn, R, t(3), name="Alice", status="ignored", reason="not_allowed")
    await add_m(conn, R, t(4), name="Alice", status="ignored", reason="denied: user")
    for n, reason in enumerate(_WITHHELD):
        await add_m(conn, R, t(20 + n), name="Alice", status="ignored", reason=reason)
    b1 = await add_m(conn, R, t(5), sender=BOB, name="Bob")
    await add_s(conn, R, t(6))
    await add_o(conn, R, t(7))
    await add_m(conn, OTHER, t(8), name="Alice")
    rows, _ = await _page(conn, sender_name="alice")
    assert _ids(rows) == [("m", a1), ("m", a2)]
    digest = hashlib.sha256(BOB.encode()).hexdigest()[:12]
    rows, _ = await _page(conn, sender_digest=digest)
    assert _ids(rows) == [("m", b1)]
    alice = hashlib.sha256(ALICE.encode()).hexdigest()[:12]
    rows, _ = await _page(conn, sender_digest=alice)
    assert _ids(rows) == [("m", a1), ("m", a2)]


async def test_has_more_flags(conn):
    ids = [await add_m(conn, R, t(i)) for i in range(5)]
    rows, more = await _page(conn, limit=2)
    assert _ids(rows) == [("m", ids[3]), ("m", ids[4])] and more is True
    oldest = await history.position(conn, R, f"m{ids[0]}")
    rows, more = await _page(conn, after=oldest, limit=10)
    assert len(rows) == 4 and more is False
    rows, more = await _page(conn, after=oldest, limit=4)
    assert len(rows) == 4 and more is False
    rows, more = await _page(conn, after=oldest, limit=3)
    assert len(rows) == 3 and more is True


async def test_feed_row_fields(conn):
    m_id = await add_m(conn, R, t(1), body="photo", media=("image", "image/png", 12, "IMG_1.png"))
    await conn.execute("UPDATE inbound SET edited_at = %s WHERE id = %s", (t(2), m_id))
    s_id = await add_s(conn, R, t(3), status="queued")
    await conn.execute(
        "INSERT INTO send_files (send_id, position, path, name, mime, msgtype, size, sha256)"
        " VALUES (%s, 0, 'out/a', 'a', 'text/plain', 'm.file', 1, %s),"
        " (%s, 1, 'out/b', 'b', 'text/plain', 'm.file', 1, %s)",
        (s_id, "0" * 64, s_id, "1" * 64),
    )
    rows, _ = await _page(conn)
    m, s = rows
    assert (m.sender, m.sender_name, m.body, m.edited, m.redacted) == (
        ALICE,
        "Alice A",
        "photo",
        True,
        False,
    )
    assert (m.media_kind, m.media_mime, m.media_size, m.media_name, m.files) == (
        "image",
        "image/png",
        12,
        "IMG_1.png",
        0,
    )
    assert (s.sender, s.body, s.status, s.files, s.media_kind) == (
        None,
        "from agent",
        "queued",
        2,
        None,
    )
    assert "mxc" not in repr(rows)
