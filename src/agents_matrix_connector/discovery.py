"""Pure room classification and timeline event intake (no I/O)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

_BRIDGE_TYPES = ("m.bridge", "uk.half-shot.bridge")
_ROOM_TYPE_KEY = "com.beeper.room_type.v2"
_INFRASTRUCTURE_TYPES = ("space", "personal_filtering_space")


@dataclass(frozen=True)
class RoomInfo:
    kind: str
    name: str | None
    alias: str | None
    encrypted: bool
    message_request: bool
    members: dict[str, str | None]
    member_count: int


def _content(event: dict) -> dict:
    content = event.get("content")
    return content if isinstance(content, dict) else {}


def classify(
    state: Iterable[dict], self_ids: frozenset[str], bridge_bots: Sequence[str]
) -> RoomInfo:
    bridge: dict | None = None
    encrypted = False
    room_name: str | None = None
    alias: str | None = None
    members: dict[str, str | None] = {}
    for event in state:
        etype = event.get("type")
        content = _content(event)
        if etype in _BRIDGE_TYPES:
            bridge = content
        elif etype == "m.room.encryption":
            encrypted = True
        elif etype == "m.room.name":
            room_name = content.get("name") or None
        elif etype == "m.room.canonical_alias":
            alias = content.get("alias") or None
        elif etype == "m.room.member":
            user_id = event.get("state_key")
            if isinstance(user_id, str) and content.get("membership") == "join":
                members[user_id] = content.get("displayname") or None

    channel: dict = {}
    if bridge is None:
        kind = "matrix"
    else:
        room_type = bridge.get(_ROOM_TYPE_KEY)
        if room_type == "dm":
            kind = "dm"
        elif room_type in _INFRASTRUCTURE_TYPES:
            kind = "infrastructure"
        else:
            kind = "group"
        raw_channel = bridge.get("channel")
        channel = raw_channel if isinstance(raw_channel, dict) else {}

    name = room_name or channel.get("displayname") or None
    if name is None:
        others = [
            display
            for user_id, display in members.items()
            if user_id not in self_ids and user_id not in bridge_bots
        ]
        if len(others) == 1:
            name = others[0]

    return RoomInfo(
        kind=kind,
        name=name,
        alias=alias,
        encrypted=encrypted,
        message_request=bool(channel.get("com.beeper.message_request")),
        members=members,
        member_count=len(members),
    )


def facebook_id(user_id: str, pattern: re.Pattern[str], bridge_server: str) -> str | None:
    """The Facebook id of a bridge ghost: `pattern` matches the whole `@localpart:` of a user
    id on `bridge_server` (mautrix-meta ghosts live on the bot's own homeserver). A ghost-like
    id on any other server has none, so a `facebook` rule never matches it."""
    localpart, separator, server = user_id.partition(":")
    if not separator or server != bridge_server:
        return None
    match = pattern.fullmatch(f"{localpart}:")
    return match.group("id") if match else None


@dataclass(frozen=True)
class Candidate:
    event_id: str
    room_id: str
    sender: str
    body: str
    formatted_body: str | None
    mention_ids: tuple[str, ...]
    origin_ts: int


@dataclass(frozen=True)
class Skip:
    event_id: str
    sender: str


@dataclass(frozen=True)
class Unsupported:
    event_id: str
    sender: str


def parse_message(
    room_id: str, event: dict, self_ids: frozenset[str], bridge_bots: Sequence[str]
) -> Candidate | Skip | Unsupported:
    event_id = str(event.get("event_id", ""))
    sender = str(event.get("sender", ""))
    skip = Skip(event_id, sender)
    if event.get("type") != "m.room.message":
        return skip
    if sender in self_ids or sender in bridge_bots:
        return skip
    content = _content(event)
    relates = content.get("m.relates_to")
    if isinstance(relates, dict) and relates.get("rel_type") == "m.replace":
        return skip
    body = content.get("body")
    if not isinstance(body, str):
        return skip
    if content.get("msgtype") not in ("m.text", "m.emote"):
        return Unsupported(event_id, sender)
    formatted = content.get("formatted_body")
    mentions = content.get("m.mentions")
    ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
    return Candidate(
        event_id=event_id,
        room_id=room_id,
        sender=sender,
        body=body,
        formatted_body=formatted if isinstance(formatted, str) else None,
        mention_ids=tuple(i for i in ids if isinstance(i, str)) if isinstance(ids, list) else (),
        origin_ts=int(event.get("origin_server_ts") or 0),
    )


MAX_ALIASES = 10
MAX_ALIAS_CHARS = 64


def normalize_aliases(values: Sequence[str]) -> tuple[str, ...]:
    """Clean a room's agent aliases: strip, drop one leading `@`, collapse whitespace, reject
    empty, overlong, control or format characters and aliases without a letter or digit; drop
    case-insensitive duplicates (first spelling wins). Raises ValueError naming the 1-based
    index."""
    seen: set[str] = set()
    result: list[str] = []
    for index, raw in enumerate(values, start=1):
        value = raw.strip()
        if value.startswith("@"):
            value = value[1:].strip()
        value = " ".join(value.split())
        if not value:
            raise ValueError(f"alias {index} is empty")
        if len(value) > MAX_ALIAS_CHARS:
            raise ValueError(f"alias {index} is longer than {MAX_ALIAS_CHARS} characters")
        if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
            raise ValueError(f"alias {index} contains a control or format character")
        if not any(ch.isalnum() for ch in value):
            raise ValueError(f"alias {index} has no letter or digit")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    if len(result) > MAX_ALIASES:
        raise ValueError(f"more than {MAX_ALIASES} aliases")
    return tuple(result)


def alias_mentioned(body: str, aliases: Sequence[str]) -> bool:
    """Whether `body` contains one of `aliases` as a whole word (casefolded, whitespace runs
    match one another; a leading `@` in the body is a non-word character)."""
    folded = body.casefold()
    for alias in aliases:
        words = alias.casefold().split()
        if not words:
            continue
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)"
        if re.search(pattern, folded):
            return True
    return False


def mentions_self(candidate: Candidate, self_ids: frozenset[str], aliases: Sequence[str]) -> bool:
    if any(i in self_ids for i in candidate.mention_ids):
        return True
    if candidate.formatted_body:
        for self_id in self_ids:
            for form in (self_id, quote(self_id, safe="")):
                if f"https://matrix.to/#/{form}" in candidate.formatted_body:
                    return True
    return alias_mentioned(candidate.body, aliases)


def effective_trigger(
    kind: str, member_count: int, room_trigger: str | None, group_default: str
) -> str:
    if room_trigger:
        return room_trigger
    if kind == "dm" or (kind == "matrix" and member_count <= 2):
        return "all"
    return group_default


def truncate(body: str, max_chars: int) -> str:
    if len(body) <= max_chars:
        return body
    return f"{body[:max_chars]} [truncated]"
