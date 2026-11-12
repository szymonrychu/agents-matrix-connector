"""Pure media helpers: content sniffing, inbound image preparation, outbound mapping, names
and workdir path rules. No I/O; callers wrap the image work in `asyncio.to_thread`.

Type never comes from a sender's mime, filename or extension: `sniff` reads the bytes, and
everything `prepare_inbound` returns is re-derived from the output bytes. Re-encoding never
copies EXIF, XMP, ICC or text chunks.
"""

import fnmatch
import io
import json
import mimetypes
import re
from dataclasses import dataclass

import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

HEIC_BRANDS = (b"heic", b"heix", b"mif1", b"msf1")
EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "application/json": "json",
    "video/mp4": "mp4",
    "audio/mp4": "m4a",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "application/octet-stream": "bin",
}
OUTBOUND_MSGTYPE = {
    "image/jpeg": "m.image",
    "image/png": "m.image",
    "image/gif": "m.image",
    "image/webp": "m.image",
    "video/mp4": "m.video",
    "video/quicktime": "m.video",
    "video/webm": "m.video",
    "audio/mpeg": "m.audio",
    "audio/mp4": "m.audio",
    "audio/wav": "m.audio",
    "audio/ogg": "m.audio",
}
_PIL_FORMATS = {
    "image/jpeg": ["JPEG"],
    "image/png": ["PNG"],
    "image/gif": ["GIF"],
    "image/webp": ["WEBP"],
    "image/heic": ["HEIF"],
}
_OFFICE = "application/vnd.openxmlformats-officedocument"
_EXTENSION_MIMES = {
    ".docx": f"{_OFFICE}.wordprocessingml.document",
    ".xlsx": f"{_OFFICE}.spreadsheetml.sheet",
    ".pptx": f"{_OFFICE}.presentationml.presentation",
    ".zip": "application/zip",
    ".csv": "text/csv",
    ".md": "text/markdown",
}
_PASSTHROUGH = ("application/pdf", "text/plain", "application/json")
_MXC_RE = "^mxc://{server}/([A-Za-z0-9_-]+)$"
_SCALE = 0.75
_MIN_EDGE = 256
_SAFE_NAME_MAX = 128
_DENY_NAMES = (".env", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "credentials*")
_PATH_MAX = 512


class MediaRefused(Exception):
    """An inbound attachment that is not delivered: `reason` is `too_large`,
    `unsupported_type` or `undecodable`."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Prepared:
    data: bytes
    mime: str
    ext: str
    w: int | None
    h: int | None


def parse_mxc(url: str, server_name: str) -> tuple[str, str] | None:
    """(server, media id) of an `mxc://` url on our own homeserver, else None."""
    match = re.fullmatch(_MXC_RE.format(server=re.escape(server_name)), url)
    return (server_name, match.group(1)) if match else None


def sniff(data: bytes) -> str:
    """The mime type of `data` from its leading bytes (and UTF-8/JSON validity for text)."""
    head = data[:12]
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in HEIC_BRANDS:
            return "image/heic"
        if brand == b"M4A ":
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if head[:5] == b"%PDF-":
        return "application/pdf"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:3] == b"ID3" or (head[:1] == b"\xff" and len(head) > 1 and head[1] & 0xF0 == 0xF0):
        return "audio/mpeg"
    if b"\x00" in data:
        return "application/octet-stream"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    try:
        value = json.loads(text)
    except ValueError:
        return "text/plain"
    return "application/json" if isinstance(value, dict | list) else "text/plain"


def prepare_inbound(data: bytes, *, max_edge: int, max_pixels: int, max_bytes: int) -> Prepared:
    """What the agent receives for inbound `data`, or MediaRefused."""
    mime = sniff(data)
    if mime in _PASSTHROUGH:
        if len(data) > max_bytes:
            raise MediaRefused("too_large")
        return Prepared(data, mime, EXT[mime], None, None)
    if mime not in _PIL_FORMATS:
        raise MediaRefused("unsupported_type")
    image = _decode(data, mime, max_edge, max_pixels)
    if mime in ("image/gif", "image/webp"):
        native = _encode_native(image, mime, max_bytes)
        if native is not None:
            return native
    return _encode_jpeg_or_png(image, max_bytes)


def _decode(data: bytes, mime: str, max_edge: int, max_pixels: int) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data), formats=_PIL_FORMATS[mime])
        if image.width * image.height > max_pixels:
            raise MediaRefused("too_large")
        image.load()
        image = ImageOps.exif_transpose(image)
        has_alpha = _has_alpha(image)
        image = image.convert("RGBA" if has_alpha else "RGB")
        image.info = {}
        if max(image.size) > max_edge:
            image = _scaled(image, max_edge / max(image.size))
    except MediaRefused:
        raise
    except Image.DecompressionBombError:
        raise MediaRefused("too_large") from None
    except Exception:
        raise MediaRefused("undecodable") from None
    return image


def _has_alpha(image: Image.Image) -> bool:
    if image.mode == "P":
        return "transparency" in image.info
    if image.mode in ("RGBA", "LA", "PA"):
        return image.getchannel("A").getextrema()[0] < 255
    return False


def _scaled(image: Image.Image, factor: float) -> Image.Image:
    width, height = image.size
    size = (max(1, round(width * factor)), max(1, round(height * factor)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _dump(image: Image.Image, fmt: str, **options) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=fmt, **options)
    return buf.getvalue()


def _encode_native(image: Image.Image, mime: str, max_bytes: int) -> Prepared | None:
    """The first frame re-encoded in its own format, or None when it does not fit."""
    if mime == "image/gif":
        attempts = [_dump(image, "GIF", optimize=True)]
    else:
        attempts = []
        for quality in (85, 75, 65):
            attempts.append(_dump(image, "WEBP", quality=quality))
            if len(attempts[-1]) <= max_bytes:
                break
    out = attempts[-1]
    if len(out) > max_bytes:
        return None
    return Prepared(out, mime, EXT[mime], *image.size)


def _encode_jpeg_or_png(image: Image.Image, max_bytes: int) -> Prepared:
    alpha = image.mode == "RGBA"
    mime = "image/png" if alpha else "image/jpeg"
    qualities = (None,) if alpha else (85, 75, 65)
    while True:
        for quality in qualities:
            out = _encode_one(image, alpha, quality)
            if len(out) <= max_bytes:
                return Prepared(out, mime, EXT[mime], *image.size)
        if max(image.size) < _MIN_EDGE:
            raise MediaRefused("too_large")
        image = _scaled(image, _SCALE)
        qualities = (None,) if alpha else (65,)


def _encode_one(image: Image.Image, alpha: bool, quality: int | None) -> bytes:
    if alpha:
        return _dump(image, "PNG", optimize=True)
    return _dump(image, "JPEG", quality=quality)


def outbound_msgtype(mime: str) -> str:
    return OUTBOUND_MSGTYPE.get(mime, "m.file")


def outbound_cap(msgtype: str, image_max: int, file_max: int) -> int:
    return image_max if msgtype == "m.image" else file_max


def outbound_mime(data: bytes, name: str) -> str:
    """The sniffed mime; an unrecognised file may take a non-media mime from its extension,
    so an extension never upgrades a file to an image, video or audio message."""
    mime = sniff(data)
    if mime != "application/octet-stream":
        return mime
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    guess = _EXTENSION_MIMES.get(ext) or mimetypes.guess_type(name)[0]
    if guess and not guess.startswith(("image/", "video/", "audio/")):
        return guess
    return mime


def strip_outbound_metadata(data: bytes, mime: str) -> tuple[bytes, int | None, int | None]:
    """(bytes, width, height) for an outbound image; metadata (JPEG Exif/XMP, PNG text and
    eXIf chunks, WebP EXIF/XMP chunks) is removed by re-encoding only when present, else the
    input bytes come back unchanged. Non-images return (data, None, None). Fails closed:
    an image that cannot be inspected or re-encoded raises MediaRefused, never the original."""
    if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return data, None, None
    try:
        image = Image.open(io.BytesIO(data), formats=_PIL_FORMATS[mime])
        size = image.size
        if mime == "image/gif" or not _has_metadata(data, mime):
            return data, *size
        if mime == "image/webp" and getattr(image, "n_frames", 1) > 1:
            return data, *size
        image.load()
        transparency = image.info.get("transparency") if mime == "image/png" else None
        image = ImageOps.exif_transpose(image)
        image.info = {}
        if mime == "image/jpeg":
            out = _dump(image, "JPEG", quality=90)
        elif mime == "image/png":
            options = {} if transparency is None else {"transparency": transparency}
            out = _dump(image, "PNG", **options)
        else:
            out = _dump(image, "WEBP", quality=90)
        return out, *image.size
    except Exception:
        raise MediaRefused("undecodable") from None


def _has_metadata(data: bytes, mime: str) -> bool:
    if mime == "image/jpeg":
        return _jpeg_has_metadata(data)
    if mime == "image/png":
        return _png_has_metadata(data)
    return _webp_has_metadata(data)


def _jpeg_has_metadata(data: bytes) -> bool:
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xDA or marker == 0xD9:
            return False
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if marker == 0xE1 and data[pos + 4 : pos + 30].startswith(
            (b"Exif\x00", b"http://ns.adobe.com/xap/1.0/")
        ):
            return True
        pos += 2 + max(length, 2)
    return False


def _png_has_metadata(data: bytes) -> bool:
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        if data[pos + 4 : pos + 8] in (b"tEXt", b"iTXt", b"zTXt", b"eXIf"):
            return True
        pos += 12 + length
    return False


def _webp_has_metadata(data: bytes) -> bool:
    pos = 12
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos + 4 : pos + 8], "little")
        if data[pos : pos + 4] in (b"EXIF", b"XMP "):
            return True
        pos += 8 + length + (length & 1)
    return False


def safe_name(path: str) -> str:
    """The basename of `path` limited to `[A-Za-z0-9._-]`, no leading dots, at most 128
    characters with the extension kept; `file` when nothing is left."""
    name = re.split(r"[/\\]", path)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")
    if len(name) > _SAFE_NAME_MAX:
        stem, dot, ext = name.rpartition(".")
        suffix = dot + ext if dot and len(ext) < 16 else ""
        name = name[: _SAFE_NAME_MAX - len(suffix)] + suffix
    return name or "file"


def check_agent_path(path: str) -> None:
    """Raise ValueError unless `path` is a plain relative workdir path an agent may send."""
    if not path or len(path) > _PATH_MAX:
        raise ValueError("path length")
    if "\x00" in path:
        raise ValueError("path has NUL")
    if path.startswith("/"):
        raise ValueError("path is absolute")
    parts = path.split("/")
    for index, part in enumerate(parts):
        if not part:
            raise ValueError("empty path component")
        if part in (".", ".."):
            raise ValueError("relative path component")
        if part.startswith(".") and not (index == 0 and part == ".attachments" and len(parts) > 1):
            raise ValueError("hidden path component")
    base = parts[-1].lower()
    if any(fnmatch.fnmatchcase(base, pattern) for pattern in _DENY_NAMES):
        raise ValueError("denied file name")
