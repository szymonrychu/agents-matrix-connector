import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from api_helpers import make_settings
from history_helpers import ALICE, BOB, add_m, add_o, add_s

from agents_matrix_connector.api.history import (
    HISTORY_NOTE,
    MAX_LIMIT,
    TEXT_CHARS,
    HistoryPage,
    history_page,
)
from agents_matrix_connector.api.schemas import ApiError
from agents_matrix_connector.db import inbound

R = "!r:x"
OTHER = "!other:x"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SHA_ALICE = hashlib.sha256(ALICE.encode()).hexdigest()[:12]


def t(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def _anonymize(conn, room: str = R) -> None:
    await conn.execute(
        "INSERT INTO room_settings (room_id, anonymize_names) VALUES (%s, true)", (room,)
    )


async def _page(conn, room: str = R, settings=None, **kw) -> HistoryPage:
    args = {"before": None, "after": None, "sender": None, "limit": 50}
    args.update(kw)
    return await history_page(conn, room, settings or make_settings(), **args)


def _dump(page: HistoryPage) -> dict:
    return page.model_dump(mode="json", by_alias=True)


async def test_page_shape_and_note(conn):
    m_id = await add_m(conn, R, t(1), body="hi")
    s_id = await add_s(conn, R, t(2), status="failed")
    o_id = await add_o(conn, R, t(3))
    dumped = _dump(await _page(conn))
    assert set(dumped) == {"room_id", "note", "messages", "has_more_before", "has_more_after"}
    assert dumped["room_id"] == R
    assert dumped["note"] == HISTORY_NOTE
    assert dumped["has_more_before"] is False and dumped["has_more_after"] is False
    m, s, o = dumped["messages"]
    assert "from" in m and "from_" not in m
    assert (m["id"], m["role"], m["from"], m["text"], m["status"]) == (
        f"m{m_id}",
        "member",
        "Alice A",
        "hi",
        None,
    )
    assert (s["id"], s["role"], s["from"], s["status"], s["files"]) == (
        f"s{s_id}",
        "bot",
        None,
        "failed",
        0,
    )
    assert (o["id"], o["role"], o["from"], o["text"], o["status"]) == (
        f"o{o_id}",
        "bot",
        None,
        "worker reply",
        "sent",
    )


async def test_text_truncated_at_4000(conn):
    await add_m(conn, R, t(1), body="x" * (TEXT_CHARS + 1))
    await add_m(conn, R, t(2), body="y" * TEXT_CHARS)
    long, exact = (await _page(conn)).messages
    assert len(long.text) == 4000 and long.truncated is True
    assert len(exact.text) == 4000 and exact.truncated is False


@pytest.mark.parametrize(
    ("reason", "withheld"),
    [
        ("not_allowed", "denied"),
        ("denied: user", "denied"),
        ("room_encrypted", "encrypted"),
        ("room_infrastructure", "infrastructure"),
        ("unsupported_content", "unsupported"),
    ],
)
async def test_withheld_rows(conn, reason, withheld):
    await add_m(
        conn,
        R,
        t(1),
        body="leak",
        status="ignored",
        reason=reason,
        media=("image", "image/png", 3, "a.png"),
    )
    (item,) = (await _page(conn)).messages
    assert item.withheld == withheld
    assert item.text is None and item.attachment is None and item.truncated is False
    assert item.from_ is None


async def test_not_withheld_reasons(conn):
    await add_m(conn, R, t(1), status="ignored", reason="history")
    (item,) = (await _page(conn)).messages
    assert item.withheld is None and item.text == "hello"


async def test_edited_and_redacted_flags(conn):
    edited = await add_m(conn, R, t(1), event_id="$ed", body="first")
    await add_m(
        conn, R, t(2), event_id="$red", body="gone", media=("file", "application/pdf", 9, "x.pdf")
    )
    assert await inbound.apply_edit(conn, R, "$ed", ALICE, "second", t(3))
    assert await inbound.apply_redaction(conn, R, "$red")
    a, b = (await _page(conn)).messages
    assert (a.id, a.text, a.edited, a.redacted) == (f"m{edited}", "second", True, False)
    assert (b.text, b.attachment, b.redacted, b.edited) == (None, None, True, False)


async def test_attachment_fields(conn):
    await add_m(conn, R, t(1), body=None, media=("image", "image/png", 12, "IMG_1.png"))
    (item,) = (await _page(conn)).messages
    assert item.attachment is not None
    assert item.attachment.model_dump() == {
        "kind": "image",
        "mime": "image/png",
        "size": 12,
        "name": "IMG_1.png",
    }


async def test_anonymized_room_has_no_identity(conn):
    await _anonymize(conn)
    await add_m(
        conn,
        R,
        t(1),
        name="Alice Smith",
        body="see photo",
        media=("image", "image/jpeg", 5, "IMG_9.jpg"),
    )
    page = await _page(conn)
    text = page.model_dump_json(by_alias=True)
    assert f"user-{SHA_ALICE}" in text
    for leaked in (ALICE, "Alice Smith", "IMG_9.jpg", "mxc://"):
        assert leaked not in text
    (item,) = page.messages
    assert item.attachment is not None and item.attachment.name is None


async def test_anonymized_from_filter(conn):
    await _anonymize(conn)
    a = await add_m(conn, R, t(1), name="Alice Smith")
    await add_m(conn, R, t(2), sender=BOB, name="Bob")
    await add_s(conn, R, t(3))
    page = await _page(conn, sender=f"user-{SHA_ALICE}")
    assert [m.id for m in page.messages] == [f"m{a}"]
    page = await _page(conn, sender=f"admin-{SHA_ALICE}")
    assert [m.id for m in page.messages] == [f"m{a}"]
    for sender in ("Alice Smith", ALICE, SHA_ALICE, f"user-{SHA_ALICE.upper()}", "user-1"):
        page = await _page(conn, sender=sender)
        assert page.messages == [] and not page.has_more_before and not page.has_more_after


async def test_name_filter_in_plain_room(conn):
    a = await add_m(conn, R, t(1), name="Alice Smith")
    await add_m(conn, R, t(2), sender=BOB, name="Bob")
    await add_o(conn, R, t(3))
    page = await _page(conn, sender="alice smith")
    assert [m.id for m in page.messages] == [f"m{a}"]


async def test_admin_role(conn):
    admins = make_settings(anonymize_admins=["111"])
    await add_m(conn, R, t(1))
    await add_m(conn, R, t(2), sender=BOB, name="Bob")
    alice, bob = (await _page(conn, settings=admins)).messages
    assert (alice.role, alice.from_) == ("admin", "Alice A")
    assert bob.role == "member"
    await _anonymize(conn)
    alice, bob = (await _page(conn, settings=admins)).messages
    assert (alice.role, alice.from_) == ("admin", f"admin-{SHA_ALICE}")
    assert bob.from_.startswith("user-")
    by_user_id = make_settings(anonymize_admins=[BOB])
    _, bob = (await _page(conn, settings=by_user_id)).messages
    assert bob.role == "admin" and bob.from_.startswith("admin-")


async def test_bad_reference_identical_for_unknown_and_foreign(conn):
    foreign = await add_m(conn, OTHER, t(1))
    await add_m(conn, R, t(2))
    errors = []
    for field in ("before", "after"):
        for ref in ("m999999", f"m{foreign}", "x1", "s1", "o1"):
            with pytest.raises(ApiError) as caught:
                await _page(conn, **{field: ref})
            errors.append((field, caught.value.status, caught.value.code, caught.value.message))
    for field, status, code, message in errors:
        assert (status, code) == (422, "bad_reference")
        assert message == (f"{field}: not a message id (m<n>, s<n> or o<n>) of this room's history")


async def test_both_cursors_invalid_request(conn):
    m_id = await add_m(conn, R, t(1))
    with pytest.raises(ApiError) as caught:
        await _page(conn, before=f"m{m_id}", after=f"m{m_id}")
    assert (caught.value.status, caught.value.code, caught.value.message) == (
        422,
        "invalid_request",
        "before, after: give at most one",
    )


async def test_cursor_flags(conn):
    ids = [await add_m(conn, R, t(i)) for i in range(5)]
    newest = await _page(conn, limit=2)
    assert [m.id for m in newest.messages] == [f"m{ids[3]}", f"m{ids[4]}"]
    assert (newest.has_more_before, newest.has_more_after) == (True, False)
    older = await _page(conn, before=f"m{ids[3]}", limit=2)
    assert [m.id for m in older.messages] == [f"m{ids[1]}", f"m{ids[2]}"]
    assert (older.has_more_before, older.has_more_after) == (True, True)
    oldest = await _page(conn, before=f"m{ids[1]}", limit=2)
    assert (oldest.has_more_before, oldest.has_more_after) == (False, True)
    newer = await _page(conn, after=f"m{ids[1]}", limit=2)
    assert [m.id for m in newer.messages] == [f"m{ids[2]}", f"m{ids[3]}"]
    assert (newer.has_more_before, newer.has_more_after) == (True, True)
    last = await _page(conn, after=f"m{ids[3]}", limit=2)
    assert (last.has_more_before, last.has_more_after) == (True, False)


async def test_limit_is_capped(conn):
    for i in range(MAX_LIMIT + 2):
        await add_m(conn, R, t(i))
    page = await _page(conn, limit=MAX_LIMIT + 50)
    assert len(page.messages) == MAX_LIMIT and page.has_more_before


async def test_no_mxc_url_in_page(conn):
    await add_m(conn, R, t(1), media=("image", "image/png", 1, "p.png"))
    await add_m(conn, R, t(2), media=("file", "application/pdf", 1, "d.pdf"), reply_to="$x")
    assert "mxc://" not in (await _page(conn)).model_dump_json(by_alias=True)
