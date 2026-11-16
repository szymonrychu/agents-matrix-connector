"""The bot attachment fetcher: claims a request, re-checks it, delivers the file as a new turn."""

import logging
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import test_api_agent as rig
from agents_common.fake_orchestrator import FakeOrchestrator
from history_helpers import add_m

from agents_matrix_connector.attachment_fetcher import AttachmentFetcher
from agents_matrix_connector.db import attachment_requests as repo
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.settings import BotSettings

TOKEN = "syt_fetch_token"
BOT = "@agents:hs"
ROOM = "!att:hs"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
MIB = 1024 * 1024
CANARY = "CANARY-FETCH-1"


def settings(**extra) -> BotSettings:
    return BotSettings(
        database_url="postgresql://unused/x",
        orchestrator_url="http://orch",
        orchestrator_token="orch-token",
        homeserver_url="http://hs",
        user_id=BOT,
        access_token=TOKEN,
        **extra,
    )


def pdf(size: int = 100) -> bytes:
    head = b"%PDF-1.4\n"
    return head + b"0" * (size - len(head))


@pytest.fixture
def hs():
    return FakeHomeserver(BOT, TOKEN)


@pytest.fixture
async def matrix(hs):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=hs.app)) as c:
        yield c


@pytest.fixture
def make(pool, fake, orchestrator, matrix):
    def build(**extra) -> AttachmentFetcher:
        return AttachmentFetcher(orchestrator, matrix, pool, settings(**extra))

    return build


async def bound(conn, fake: FakeOrchestrator, room=ROOM, members=(rig.ALICE,)) -> uuid.UUID:
    session = uuid.uuid4()
    fake.sessions[str(session)] = {"id": str(session), "desired": "running"}
    await rig.add_room(conn, room, members=members)
    await rig.bind(conn, room, session)
    return session


async def media_row(conn, hs, data, *, name="doc.pdf", mime="application/pdf", media_id="doc"):
    url = hs.put_media("hs", media_id, data)
    m = await add_m(
        conn, ROOM, NOW, media=("file", mime, len(data), name), sender=rig.ALICE, body=CANARY
    )
    await conn.execute("UPDATE inbound SET media_url = %s WHERE id = %s", (url, m))
    return m


async def queue(conn, session, m) -> int:
    return await repo.insert(conn, session_id=session, slug="hive-1", room_id=ROOM, inbound_id=m)


async def state(conn, rid=1):
    r = await repo.get(conn, rid)
    return r.status, r.reason, r.message_id


async def test_delivers_file_as_new_turn(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await make().tick()
    status, reason, message_id = await state(conn)
    assert (status, reason) == ("sent", None) and message_id == "1"
    msg = fake.messages[1]
    assert msg["session_id"] == str(session) and msg["from_user"] is None
    assert f'for="m{m}"' in msg["text"]
    assert [a.name for a in fake.attachments[1]] == [f"m{m}.pdf"]
    assert fake.attachments[1][0].mime == "application/pdf"
    assert 'original_name="doc.pdf"' in msg["text"]


async def test_binding_changed_is_refused(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await conn.execute("UPDATE room_bindings SET released_at = now(), release_reason = 'x'")
    await make().tick()
    assert await state(conn) == ("refused", "room_unbound", None)
    assert hs.media_downloads == [] and fake.messages == {}


async def test_rebound_to_other_session_is_refused(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await conn.execute("UPDATE room_bindings SET session_id = %s", (uuid.uuid4(),))
    await make().tick()
    assert await state(conn) == ("refused", "room_unbound", None)


async def test_redacted_row_is_refused(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await conn.execute("UPDATE inbound SET redacted = true WHERE id = %s", (m,))
    await make().tick()
    assert await state(conn) == ("refused", "no_attachment", None)
    assert hs.media_downloads == []


async def test_oversize_fails_too_large(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf(7 * MIB))
    await queue(conn, session, m)
    await make().tick()
    assert await state(conn) == ("failed", "too_large", None)
    assert fake.messages == {}


async def test_unsupported_type_fails(conn, hs, fake, make):
    session = await bound(conn, fake)
    zipped = b"PK\x03\x04\x00\xff\xfe" + bytes(range(128, 228))
    m = await media_row(conn, hs, zipped, name="a.zip", mime="application/zip")
    await queue(conn, session, m)
    await make().tick()
    assert await state(conn) == ("failed", "unsupported_type", None)
    assert fake.messages == {}


async def test_session_gone(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    fake.fail_next("post_message", 404)
    await make().tick()
    assert await state(conn) == ("failed", "session_gone", None)


async def test_orchestrator_refused_and_unavailable(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    fake.fail_next("post_message", 409)
    f = make()
    await f.tick()
    assert await state(conn) == ("failed", "orchestrator_refused", None)
    await queue(conn, session, m)
    fake.fail_next("post_message", 500)
    await f.tick()
    assert (await state(conn, 2))[:2] == ("failed", "orchestrator_unavailable")


async def test_interrupted_rows_failed_on_first_tick(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await conn.execute("UPDATE attachment_requests SET status = 'fetching', claimed_at = now()")
    await make().tick()
    assert await state(conn) == ("failed", "interrupted", None)
    assert fake.messages == {}


async def test_one_request_per_tick(conn, hs, fake, make):
    session = await bound(conn, fake)
    m1 = await media_row(conn, hs, pdf(), media_id="a")
    m2 = await media_row(conn, hs, pdf(), media_id="b")
    await queue(conn, session, m1)
    await queue(conn, session, m2)
    f = make()
    await f.tick()
    assert (await state(conn, 1))[0] == "sent" and (await state(conn, 2))[0] == "queued"
    await f.tick()
    assert (await state(conn, 2))[0] == "sent"


async def test_no_request_is_a_noop(conn, fake, make):
    await make().tick()
    assert fake.messages == {}


async def test_anonymized_delivery(conn, hs, fake, make, pool):
    from agents_matrix_connector.db import rules

    session = await bound(conn, fake)
    await rules.set_room_settings(pool, ROOM, preset_id=None, trigger=None, anonymize_names=True)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await make().tick()
    text = fake.messages[1]["text"]
    assert 'from="user-' in text and "original_name" not in text
    assert "Alice A" not in text and rig.ALICE not in text and "doc.pdf" not in text


async def test_admin_flag(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await make(anonymize_admins=[rig.ALICE]).tick()
    assert ' role="admin"' in fake.messages[1]["text"]


async def test_non_admin_has_no_role(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await make().tick()
    assert "role=" not in fake.messages[1]["text"]


async def test_logs_never_carry_body_or_name(conn, hs, fake, make, caplog):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf(), name="secret-name.pdf")
    await queue(conn, session, m)
    with caplog.at_level(logging.DEBUG):
        await make().tick()
    assert "attachment a1: sent" in caplog.text
    assert CANARY not in caplog.text and "secret-name" not in caplog.text
    assert "mxc://" not in caplog.text


async def test_unexpected_error_settles_failed_and_reraises(conn, hs, fake, make, monkeypatch):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("agents_matrix_connector.attachment_fetcher.inbound_media.attach", boom)
    with pytest.raises(RuntimeError, match="boom"):
        await make().tick()
    assert await state(conn) == ("failed", "fetch_error", None)


async def test_settle_failure_does_not_mask_original(conn, hs, fake, make, monkeypatch):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    async def bad_finish(*args, **kwargs):
        raise ValueError("settle failed")

    monkeypatch.setattr("agents_matrix_connector.attachment_fetcher.inbound_media.attach", boom)
    monkeypatch.setattr("agents_matrix_connector.attachment_fetcher.requests.finish", bad_finish)
    with pytest.raises(RuntimeError, match="boom"):
        await make().tick()


async def test_foreign_media_fails(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await conn.execute(
        "UPDATE inbound SET media_url = 'mxc://other.example/doc' WHERE id = %s", (m,)
    )
    await queue(conn, session, m)
    await make().tick()
    assert await state(conn) == ("failed", "foreign_media", None)
    assert hs.media_downloads == [] and fake.messages == {}


async def test_download_failed(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    hs.fail_media("doc", 500)
    await queue(conn, session, m)
    await make().tick()
    assert await state(conn) == ("failed", "download_failed", None)
    assert fake.messages == {}


async def test_missing_media_url_is_refused(conn, hs, fake, make):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await conn.execute("UPDATE inbound SET media_url = NULL WHERE id = %s", (m,))
    await queue(conn, session, m)
    await make().tick()
    assert await state(conn) == ("refused", "no_attachment", None)


@pytest.mark.parametrize(
    "reason",
    ["not_allowed", "denied: user", "room_encrypted", "room_infrastructure", "unsupported_content"],
)
async def test_withheld_row_is_refused_without_download(conn, hs, fake, make, reason):
    session = await bound(conn, fake)
    m = await media_row(conn, hs, pdf())
    await queue(conn, session, m)
    await conn.execute(
        "UPDATE inbound SET status = 'ignored', reason = %s WHERE id = %s", (reason, m)
    )
    await make().tick()
    assert await state(conn) == ("refused", "no_attachment", None)
    assert hs.media_downloads == [] and fake.messages == {}
