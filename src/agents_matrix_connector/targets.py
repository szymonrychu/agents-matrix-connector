"""Parse the target of a room request: a Matrix id, a Facebook id or profile link, or a name.

Pure: no I/O. The bridge accepts only numeric Facebook ids (and ghost user ids), so a link is
reduced to its `profile.php?id=` number; a vanity `facebook.com/<username>` link cannot be
resolved without the bridge's search and is refused with `VanityLink`, asking for the name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from agents_matrix_connector.discovery import facebook_id
from agents_matrix_connector.settings import USER_ID_RE

FACEBOOK_HOSTS = frozenset(
    {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "mobile.facebook.com",
        "web.facebook.com",
        "fb.com",
    }
)
MAX_NAME_CHARS = 200
_DIGITS = re.compile(r"[0-9]+", re.ASCII)


@dataclass(frozen=True)
class Target:
    """matrix: a user id; facebook: a numeric id string; name: a stripped display name."""

    kind: Literal["matrix", "facebook", "name"]
    value: str


class InvalidTarget(ValueError):
    """Not a usable target."""


class VanityLink(ValueError):
    """A `facebook.com/<username>` link: only a name search could resolve it."""


def _looks_like_url(text: str) -> bool:
    if "://" in text:
        return True
    if any(ch.isspace() for ch in text):
        return False
    host = text.split("/", 1)[0].split("?", 1)[0].lower()
    return host in FACEBOOK_HOSTS or ("/" in text and "." in host)


def _from_url(text: str) -> str:
    """The numeric Facebook id of a profile link."""
    parts = urlsplit(text if "://" in text else f"https://{text}")
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidTarget("not an http(s) link")
    host = (parts.hostname or "").lower()
    if host not in FACEBOOK_HOSTS:
        raise InvalidTarget("not a Facebook link")
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        raise InvalidTarget("a Facebook link without a profile")
    if segments[0].lower() != "profile.php":
        raise VanityLink(segments[0])
    ids = parse_qs(parts.query).get("id", [])
    value = ids[0].strip().rstrip("/") if ids else ""
    if not _DIGITS.fullmatch(value):
        raise InvalidTarget("profile.php needs a numeric id")
    return value


def parse_target(
    text: str, *, puppet_pattern: str, bridge_server: str, refused: frozenset[str]
) -> Target:
    """Classify `text`; `refused` holds the bot, its self identities and the bridge bots.

    A ghost of a refused user (and its numeric id) is refused too: the bot never opens a chat
    with itself.
    """
    pattern = re.compile(puppet_pattern)
    refused_ids = {i for u in refused if (i := facebook_id(u, pattern, bridge_server))}
    text = text.strip()
    if not text:
        raise InvalidTarget("empty target")
    if text.startswith("@"):
        if not USER_ID_RE.match(text):
            raise InvalidTarget("not a Matrix user id (@name:server)")
        if text in refused:
            raise InvalidTarget("the bot, a self identity or a bridge bot")
        ghost = facebook_id(text, pattern, bridge_server)
        return Target("facebook", ghost) if ghost else Target("matrix", text)
    if _DIGITS.fullmatch(text):
        value = text
    elif _looks_like_url(text):
        value = _from_url(text)
    else:
        if len(text) > MAX_NAME_CHARS:
            raise InvalidTarget(f"a name has at most {MAX_NAME_CHARS} characters")
        return Target("name", text)
    if value in refused_ids:
        raise InvalidTarget("the bot, a self identity or a bridge bot")
    return Target("facebook", value)
