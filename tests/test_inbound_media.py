import io
import logging
from datetime import UTC, datetime

import httpx
import pytest
from PIL import Image

from agents_matrix_connector.db.inbound import InboundRow
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.inbound_media import attach
from agents_matrix_connector.matrix import MatrixClient, MatrixUnavailable
from agents_matrix_connector.settings import BotSettings

TOKEN = "syt_inbound_media_token"
BOT = "@agents:hs"
SERVER = "hs"
MIB = 1024 * 1024


def make_settings(**overrides) -> BotSettings:
    values = dict(
        database_url="postgresql://unused/x",
        orchestrator_url="http://orch",
        orchestrator_token="orch-token",
        homeserver_url="http://hs",
        user_id=BOT,
        access_token=TOKEN,
    )
    values.update(overrides)
    return BotSettings(**values)


@pytest.fixture
def hs() -> FakeHomeserver:
    return FakeHomeserver(BOT, TOKEN)


@pytest.fixture
async def matrix(hs):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=hs.app)) as c:
        yield c


def row(id_, url, kind="image", mime="image/jpeg", size=None, name="IMG.jpg", body=""):
    return InboundRow(
        id=id_,
        event_id=f"$e{id_}",
        room_id="!r:hs",
        sender="@facebook_1:hs",
        sender_name="Ann",
        body=body,
        mentioned=False,
        origin_ts=datetime(2026, 11, 12, tzinfo=UTC),
        received_at=datetime(2026, 11, 12, tzinfo=UTC),
        status="in-progress",
        reason=None,
        run_id=1,
        media_kind=kind,
        media_url=url,
        media_mime=mime,
        media_size=size,
        media_name=name,
    )


def image_bytes(w: int, h: int, fmt: str = "JPEG") -> bytes:
    image = Image.new("RGB", (w, h), (200, 30, 30))
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    return buf.getvalue()


def pdf(size: int) -> bytes:
    head = b"%PDF-1.4\n"
    return head + b"0" * (size - len(head))


async def test_attach_image_jpeg_named_by_row(hs, matrix):
    url = hs.put_media(SERVER, "photo", image_bytes(3000, 2000))
    [outcome] = await attach([row(7, url)], matrix, make_settings(), SERVER)
    assert outcome.row_id == 7 and outcome.reason is None and outcome.kind == "image"
    assert outcome.attachment.name == "m7.jpg"
    assert outcome.attachment.mime == "image/jpeg" == outcome.mime
    assert outcome.size == len(outcome.attachment.data)
    image = Image.open(io.BytesIO(outcome.attachment.data))
    assert image.format == "JPEG" and max(image.size) == 2576


async def test_attach_pdf_native(hs, matrix):
    data = pdf(2048)
    url = hs.put_media(SERVER, "doc", data)
    [outcome] = await attach(
        [row(3, url, kind="file", mime="x/y")], matrix, make_settings(), SERVER
    )
    assert outcome.attachment.name == "m3.pdf"
    assert outcome.attachment.mime == "application/pdf"
    assert outcome.attachment.data == data


async def test_attach_gif_webp_native(hs, matrix):
    gif = hs.put_media(SERVER, "g", image_bytes(40, 30, "GIF"))
    webp = hs.put_media(SERVER, "w", image_bytes(40, 30, "WEBP"))
    rows = [row(1, gif, kind="image"), row(2, webp, kind="sticker")]
    first, second = await attach(rows, matrix, make_settings(), SERVER)
    assert (first.attachment.name, first.attachment.mime) == ("m1.gif", "image/gif")
    assert (second.attachment.name, second.attachment.mime) == ("m2.webp", "image/webp")
    assert second.kind == "sticker"


async def test_attach_video_not_downloaded(hs, matrix):
    url = hs.put_media(SERVER, "v", b"\x00\x00\x00\x18ftypmp42")
    audio = hs.put_media(SERVER, "a", b"OggS0000")
    rows = [row(1, url, kind="video", mime="video/mp4", size=10), row(2, audio, kind="audio")]
    outcomes = await attach(rows, matrix, make_settings(), SERVER)
    assert [(o.attachment, o.reason) for o in outcomes] == [
        (None, "unsupported_type"),
        (None, "unsupported_type"),
    ]
    assert (outcomes[0].mime, outcomes[0].size) == ("video/mp4", 10)
    assert hs.media_downloads == []


async def test_attach_foreign_media_not_downloaded(hs, matrix):
    rows = [
        row(1, "mxc://evil.example/abc"),
        row(2, "https://hs/abc"),
        row(3, "mxc://hs/../x"),
        row(4, None),
    ]
    outcomes = await attach(rows, matrix, make_settings(), SERVER)
    assert {o.reason for o in outcomes} == {"foreign_media"}
    assert len(outcomes) == 4
    assert hs.media_downloads == []


async def test_attach_declared_too_large_not_downloaded(hs, matrix):
    url = hs.put_media(SERVER, "big", pdf(100))
    settings = make_settings(inbound_media_max_bytes=1000)
    [outcome] = await attach([row(1, url, size=1001)], matrix, settings, SERVER)
    assert (outcome.attachment, outcome.reason) == (None, "too_large")
    assert hs.media_downloads == []


async def test_attach_actual_too_large_stops_download(hs, matrix):
    url = hs.put_media(SERVER, "big", pdf(5000))
    settings = make_settings(inbound_media_max_bytes=1000)
    [outcome] = await attach([row(1, url, size=10)], matrix, settings, SERVER)
    assert outcome.reason == "too_large"


async def test_attach_undecodable_image(hs, matrix):
    url = hs.put_media(SERVER, "bad", b"\xff\xd8\xff" + b"garbage" * 10)
    [outcome] = await attach([row(1, url)], matrix, make_settings(), SERVER)
    assert outcome.reason == "undecodable"


async def test_attach_batch_limit_first_fit(hs, matrix):
    rows = [row(i, hs.put_media(SERVER, f"p{i}", pdf(3 * MIB)), kind="file") for i in (1, 2, 3)]
    outcomes = await attach(rows, matrix, make_settings(), SERVER)
    assert [o.reason for o in outcomes] == [None, None, "batch_limit"]
    assert outcomes[2].attachment is None

    small = image_bytes(20, 20)
    rows = [row(10 + i, hs.put_media(SERVER, f"s{i}", small)) for i in range(9)]
    outcomes = await attach(rows, matrix, make_settings(), SERVER)
    assert [o.attachment is not None for o in outcomes] == [True] * 8 + [False]
    assert outcomes[8].reason == "batch_limit"


async def test_attach_rows_without_media_skipped_in_id_order(hs, matrix):
    url = hs.put_media(SERVER, "p", pdf(100))
    plain = row(1, None, kind=None)
    outcomes = await attach([row(5, url), plain, row(2, url)], matrix, make_settings(), SERVER)
    assert [o.row_id for o in outcomes] == [2, 5]


async def test_attach_download_failure_retried_once(hs, matrix):
    hs.put_media(SERVER, "flaky", pdf(100))
    hs.fail_media("flaky", 500)
    [outcome] = await attach([row(1, "mxc://hs/flaky")], matrix, make_settings(), SERVER)
    assert (outcome.attachment, outcome.reason) == (None, "download_failed")
    assert hs.media_downloads == ["flaky", "flaky"]


async def test_attach_download_succeeds_on_retry(hs, matrix, monkeypatch):
    url = hs.put_media(SERVER, "ok", pdf(100))
    real = matrix.download_media
    calls = []

    async def flaky(server, media_id, max_bytes):
        calls.append(media_id)
        if len(calls) == 1:
            raise MatrixUnavailable(None, None, "down")
        return await real(server, media_id, max_bytes)

    monkeypatch.setattr(matrix, "download_media", flaky)
    [outcome] = await attach([row(1, url)], matrix, make_settings(), SERVER)
    assert outcome.attachment is not None and len(calls) == 2


async def test_attach_canary_caption_not_logged(hs, matrix, caplog):
    caplog.set_level(logging.DEBUG)
    canary = "CANARY-inbound-7f3a"
    good = hs.put_media(SERVER, "good", image_bytes(30, 30))
    hs.fail_media("bad", 500)
    rows = [
        row(1, good, name=f"{canary}.jpg", body=f"caption {canary}"),
        row(2, "mxc://hs/bad", name=f"{canary}.pdf", body=canary),
        row(3, f"mxc://evil/{canary}", name=canary),
    ]
    await attach(rows, matrix, make_settings(), SERVER)
    assert canary not in caplog.text
