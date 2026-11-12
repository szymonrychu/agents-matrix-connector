"""Pure media helpers: sniffing, inbound preparation, outbound mapping, names, path rules."""

import io
import os
import struct
import zlib

import pytest
from PIL import Image

from agents_matrix_connector import media
from agents_matrix_connector.media import MediaRefused

SERVER = "szymonrichert.pl"


def _save(image: Image.Image, fmt: str, **kwargs) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def _gradient(w: int, h: int, mode: str = "RGB") -> Image.Image:
    image = Image.new("RGB", (w, h))
    image.putdata([(x % 256, y % 256, (x + y) % 256) for y in range(h) for x in range(w)])
    return image.convert(mode)


def _noise(w: int, h: int) -> Image.Image:
    return Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def _ftyp(brand: bytes) -> bytes:
    return struct.pack(">I", 24) + b"ftyp" + brand + b"\x00\x00\x00\x00" + brand + b"isom"


def _heic() -> bytes:
    import pillow_heif

    return _save_heif(pillow_heif, _gradient(64, 48))


def _save_heif(pillow_heif, image: Image.Image) -> bytes:
    buf = io.BytesIO()
    pillow_heif.from_pillow(image).save(buf, format="HEIF")
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _exif(orientation: int | None = None, gps: bool = False) -> Image.Exif:
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    if gps:
        exif.get_ifd(0x8825)[1] = "N"
        exif.get_ifd(0x8825)[2] = (52.0, 13.0, 0.0)
    return exif


PREP = {"max_edge": 2576, "max_pixels": 40_000_000, "max_bytes": 6_291_456}


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"\xff\xd8\xff\xe0abc", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\nxxxx", "image/png"),
        (b"GIF87a....", "image/gif"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (_ftyp(b"heic"), "image/heic"),
        (_ftyp(b"heix"), "image/heic"),
        (_ftyp(b"mif1"), "image/heic"),
        (_ftyp(b"msf1"), "image/heic"),
        (b"%PDF-1.7\n", "application/pdf"),
        (_ftyp(b"isom"), "video/mp4"),
        (_ftyp(b"M4A "), "audio/mp4"),
        (_ftyp(b"qt  "), "video/quicktime"),
        (b"\x1a\x45\xdf\xa3rest", "video/webm"),
        (b"OggSrest", "audio/ogg"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
        (b"ID3\x04rest", "audio/mpeg"),
        (b"\xff\xfbrest", "audio/mpeg"),
        (b"{}", "application/json"),
        (b'[1, "a"]', "application/json"),
        (b"hello world", "text/plain"),
        (b"3", "text/plain"),
        (b"a\x00b", "application/octet-stream"),
        (b"\xfe\xfd\xfc\x00", "application/octet-stream"),
        (b"", "text/plain"),
    ],
)
def test_sniff_table(data, mime):
    assert media.sniff(data) == mime


def test_sniff_ignores_labels_png_named_jpg():
    assert media.sniff(_save(_gradient(8, 8), "PNG")) == "image/png"


def test_parse_mxc():
    assert media.parse_mxc(f"mxc://{SERVER}/abc_DEF-1", SERVER) == (SERVER, "abc_DEF-1")
    assert media.parse_mxc("mxc://other.org/abc", SERVER) is None
    assert media.parse_mxc(f"mxc://{SERVER}/../x", SERVER) is None
    assert media.parse_mxc(f"mxc://{SERVER}/a/b", SERVER) is None
    assert media.parse_mxc(f"mxc://{SERVER}/", SERVER) is None
    assert media.parse_mxc(f"mxc://{SERVER}x/abc", SERVER) is None
    assert media.parse_mxc(f"https://{SERVER}/abc", SERVER) is None
    assert media.parse_mxc(f"mxc://{SERVER}/abc\n", SERVER) is None


def test_prepare_downscales_to_max_edge():
    out = media.prepare_inbound(_save(_gradient(3000, 2000), "JPEG"), **PREP)
    assert (out.mime, out.ext) == ("image/jpeg", "jpg")
    assert (out.w, out.h) == (2576, 1717)
    assert _open(out.data).size == (2576, 1717)
    assert media.sniff(out.data) == "image/jpeg"


def test_prepare_exif_orientation_applied_and_stripped():
    source = _save(_gradient(4032, 3024), "JPEG", exif=_exif(orientation=6, gps=True))
    assert Image.open(io.BytesIO(source)).getexif().get_ifd(0x8825)
    out = media.prepare_inbound(source, **PREP)
    image = _open(out.data)
    assert image.height > image.width
    assert max(image.size) == 2576
    assert len(image.getexif()) == 0
    assert (out.w, out.h) == image.size


def test_prepare_png_alpha_stays_png():
    image = _gradient(300, 200, "RGBA")
    image.putalpha(128)
    out = media.prepare_inbound(_save(image, "PNG"), **PREP)
    assert (out.mime, out.ext) == ("image/png", "png")
    assert _open(out.data).mode == "RGBA"


def test_prepare_png_without_alpha_becomes_jpeg():
    out = media.prepare_inbound(_save(_gradient(300, 200), "PNG"), **PREP)
    assert (out.mime, out.ext) == ("image/jpeg", "jpg")
    assert _open(out.data).mode == "RGB"


def test_prepare_steps_quality_down_to_fit():
    noise = _noise(2576, 2576)
    source = _save(noise, "PNG")
    q85 = _save(noise, "JPEG", quality=85)
    cap = int(len(q85) * 0.7)
    out = media.prepare_inbound(source, max_edge=2576, max_pixels=40_000_000, max_bytes=cap)
    assert len(out.data) <= cap
    assert _open(out.data).size[0] <= 2576
    tight = media.prepare_inbound(
        source, max_edge=2576, max_pixels=40_000_000, max_bytes=len(q85) // 8
    )
    assert len(tight.data) <= len(q85) // 8
    assert max(_open(tight.data).size) < 2576


def test_prepare_too_big_even_when_tiny_raises():
    with pytest.raises(MediaRefused) as err:
        media.prepare_inbound(
            _save(_noise(600, 600), "PNG"), max_edge=2576, max_pixels=10**8, max_bytes=500
        )
    assert err.value.reason == "too_large"


def test_prepare_heic_to_jpeg():
    out = media.prepare_inbound(_heic(), **PREP)
    assert (out.mime, out.ext) == ("image/jpeg", "jpg")
    assert _open(out.data).size == (64, 48)


def test_prepare_gif_and_webp_native():
    frames = [
        _gradient(120, 80),
        _gradient(120, 80).rotate(90, expand=True).resize((120, 80)),
        _gradient(120, 80).transpose(Image.Transpose.FLIP_LEFT_RIGHT),
    ]
    gif = _save(frames[0], "GIF", save_all=True, append_images=frames[1:])
    assert getattr(Image.open(io.BytesIO(gif)), "n_frames", 1) == 3
    out = media.prepare_inbound(gif, **PREP)
    assert (out.mime, out.ext) == ("image/gif", "gif")
    assert getattr(_open(out.data), "n_frames", 1) == 1

    rgba = _gradient(100, 100, "RGBA")
    rgba.putalpha(100)
    out = media.prepare_inbound(_save(rgba, "WEBP"), **PREP)
    assert (out.mime, out.ext) == ("image/webp", "webp")
    assert _open(out.data).mode == "RGBA"


def test_prepare_gif_larger_than_cap_falls_back():
    gif = _save(_noise(300, 300).convert("P"), "GIF")
    out = media.prepare_inbound(gif, max_edge=2576, max_pixels=10**8, max_bytes=len(gif) // 2)
    assert out.mime in ("image/png", "image/jpeg")
    assert len(out.data) <= len(gif) // 2


def test_prepare_refuses_pixel_bomb_from_header(monkeypatch):
    header = struct.pack(">IIBBBBB", 30000, 30000, 8, 2, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IEND", b"")

    def boom(self, *args, **kwargs):
        raise AssertionError("load called")

    monkeypatch.setattr(Image.Image, "load", boom)
    with pytest.raises(MediaRefused) as err:
        media.prepare_inbound(data, **PREP)
    assert err.value.reason == "too_large"


def test_prepare_truncated_jpeg_undecodable():
    data = _save(_noise(400, 400), "JPEG")
    with pytest.raises(MediaRefused) as err:
        media.prepare_inbound(data[: len(data) // 2], **PREP)
    assert err.value.reason == "undecodable"


def test_prepare_pdf_passes_and_caps():
    pdf = b"%PDF-1.4\n" + b"x" * 100
    out = media.prepare_inbound(pdf, **PREP)
    assert (out.data, out.mime, out.ext, out.w, out.h) == (
        pdf,
        "application/pdf",
        "pdf",
        None,
        None,
    )
    with pytest.raises(MediaRefused) as err:
        media.prepare_inbound(pdf, max_edge=2576, max_pixels=10**8, max_bytes=50)
    assert err.value.reason == "too_large"


def test_prepare_text_and_json():
    out = media.prepare_inbound(b"hello", **PREP)
    assert (out.mime, out.ext) == ("text/plain", "txt")
    out = media.prepare_inbound(b'{"a": 1}', **PREP)
    assert (out.mime, out.ext) == ("application/json", "json")
    with pytest.raises(MediaRefused) as err:
        media.prepare_inbound(b"x" * 100, max_edge=2576, max_pixels=10**8, max_bytes=50)
    assert err.value.reason == "too_large"


def test_prepare_video_unsupported():
    with pytest.raises(MediaRefused) as err:
        media.prepare_inbound(_ftyp(b"isom") + b"\x00" * 64, **PREP)
    assert err.value.reason == "unsupported_type"


def test_prepare_octet_unsupported():
    with pytest.raises(MediaRefused) as err:
        media.prepare_inbound(b"\x00\x01\x02\x03", **PREP)
    assert err.value.reason == "unsupported_type"


def test_ext_table():
    assert media.EXT["image/jpeg"] == "jpg"
    assert media.EXT["application/octet-stream"] == "bin"
    assert media.EXT["audio/mpeg"] == "mp3"


def test_outbound_msgtype_and_cap():
    assert media.outbound_msgtype("image/png") == "m.image"
    assert media.outbound_msgtype("image/webp") == "m.image"
    assert media.outbound_msgtype("video/quicktime") == "m.video"
    assert media.outbound_msgtype("audio/ogg") == "m.audio"
    assert media.outbound_msgtype("application/pdf") == "m.file"
    assert media.outbound_msgtype("image/heic") == "m.file"
    assert media.outbound_cap("m.image", 8, 25) == 8
    assert media.outbound_cap("m.video", 8, 25) == 25
    assert media.outbound_cap("m.file", 8, 25) == 25


def test_outbound_mime_extension_fallback():
    assert media.outbound_mime(b"\x00\x01\x02", "x.docx").endswith("wordprocessingml.document")
    assert media.outbound_mime(b"PK\x03\x04\x00", "x.png") == "application/octet-stream"
    assert media.outbound_mime(b"\x00\x01", "x.mp4") == "application/octet-stream"
    assert media.outbound_mime(b"\x00\x01", "noext") == "application/octet-stream"
    assert media.outbound_mime(_save(_gradient(8, 8), "PNG"), "x.bin") == "image/png"


def test_strip_outbound_metadata():
    with_gps = _save(_gradient(100, 80), "JPEG", exif=_exif(gps=True))
    data, w, h = media.strip_outbound_metadata(with_gps, "image/jpeg")
    assert (w, h) == (100, 80)
    assert data != with_gps
    assert len(Image.open(io.BytesIO(data)).getexif()) == 0

    clean = _save(_gradient(50, 40), "PNG")
    data, w, h = media.strip_outbound_metadata(clean, "image/png")
    assert data == clean and (w, h) == (50, 40)

    clean_jpeg = _save(_gradient(50, 40), "JPEG")
    assert media.strip_outbound_metadata(clean_jpeg, "image/jpeg")[0] == clean_jpeg

    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Author", "secret")
    tagged = _save(_gradient(50, 40), "PNG", pnginfo=info)
    data, _, _ = media.strip_outbound_metadata(tagged, "image/png")
    assert b"secret" not in data and not _open(data).info.get("Author")

    webp = _save(_gradient(50, 40), "WEBP", exif=_exif(gps=True))
    data, w, h = media.strip_outbound_metadata(webp, "image/webp")
    assert b"EXIF" not in data and (w, h) == (50, 40)

    assert media.strip_outbound_metadata(b"%PDF-1.4", "application/pdf") == (
        b"%PDF-1.4",
        None,
        None,
    )


def test_safe_name():
    assert media.safe_name("out/My Chart (1).png") == "My_Chart__1_.png"
    long = media.safe_name("a" * 300 + ".png")
    assert len(long) == 128 and long.endswith(".png")
    assert media.safe_name("...hidden") == "hidden"
    assert media.safe_name("") == "file"
    assert media.safe_name("a/b/") == "file"
    assert media.safe_name("..") == "file"


@pytest.mark.parametrize("path", ["out/a.png", ".attachments/orch-5/m42.jpg"])
def test_check_agent_path_ok(path):
    media.check_agent_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "/etc/passwd",
        "a/../b",
        ".env",
        ".git/config",
        "x/.hidden",
        "keys/id_rsa",
        "keys/id_ed25519.pub",
        "c.PEM",
        "d.key",
        "credentials.json",
        "a\x00b",
        "a" * 513,
        "a//b",
        "",
    ],
)
def test_check_agent_path_refused(path):
    with pytest.raises(ValueError):
        media.check_agent_path(path)


def test_prepare_palette_png_with_transparency_stays_png():
    image = Image.new("P", (40, 30), 0)
    image.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    data = _save(image, "PNG", transparency=0)
    out = media.prepare_inbound(data, max_edge=2000, max_pixels=10_000_000, max_bytes=1_000_000)
    assert out.mime == "image/png"
    assert Image.open(io.BytesIO(out.data)).convert("RGBA").getchannel("A").getextrema()[0] == 0


def test_strip_outbound_metadata_fails_closed():
    broken = _save(_gradient(40, 30), "JPEG", exif=_exif(gps=True))[:-300]
    with pytest.raises(MediaRefused):
        media.strip_outbound_metadata(broken, "image/jpeg")


def test_decode_does_not_mutate_pil_globals():
    before = Image.MAX_IMAGE_PIXELS
    data = _save(_gradient(20, 20), "PNG")
    media.prepare_inbound(data, max_edge=2000, max_pixels=123_456, max_bytes=1_000_000)
    assert before == Image.MAX_IMAGE_PIXELS
