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
class Media:
    """The attachment of a media event as the sender declared it: everything but `kind` is
    sender text, never trusted for the type (the worker sniffs the bytes)."""

    kind: str
    url: str
    mime: str | None
    size: int | None
    name: str | None
    w: int | None
    h: int | None


@dataclass(frozen=True)
class Candidate:
    event_id: str
    room_id: str
    sender: str
    body: str
    formatted_body: str | None
    mention_ids: tuple[str, ...]
    origin_ts: int
    media: Media | None = None
    reply_to: str | None = None
    edited_body: str | None = None
    edited_ts: int | None = None


@dataclass(frozen=True)
class Edit:
    """An `m.replace` event: the new text of `target`, by `sender`."""

    event_id: str
    room_id: str
    sender: str
    target: str
    body: str
    origin_ts: int


@dataclass(frozen=True)
class Redaction:
    event_id: str
    target: str


@dataclass(frozen=True)
class Redacted:
    """A message event the server already redacted (it arrives without its content)."""

    event_id: str
    sender: str


@dataclass(frozen=True)
class Skip:
    event_id: str
    sender: str


@dataclass(frozen=True)
class Unsupported:
    event_id: str
    sender: str


MESSAGE_TYPES = ("m.room.message", "m.sticker")
_TEXT_MSGTYPES = ("m.text", "m.emote")
_MEDIA_MSGTYPES = {"m.image": "image", "m.file": "file", "m.video": "video", "m.audio": "audio"}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _media(content: dict, kind: str, body: str) -> tuple[str, Media] | None:
    """The row body and the media part of a media event; None without a plain string `url`
    (an encrypted `file` object is not supported). Caption rule (Matrix v1.10): a `filename`
    that differs from `body` makes `body` the caption, else `body` is the file name."""
    url = content.get("url")
    if not isinstance(url, str):
        return None
    filename = content.get("filename")
    if isinstance(filename, str) and filename != body:
        caption, name = body, filename
    else:
        caption, name = "", body
    raw_info = content.get("info")
    info = raw_info if isinstance(raw_info, dict) else {}
    mime = info.get("mimetype")
    return caption, Media(
        kind=kind,
        url=url,
        mime=mime if isinstance(mime, str) else None,
        size=_int(info.get("size")),
        name=name,
        w=_int(info.get("w")),
        h=_int(info.get("h")),
    )


def strip_reply_fallback(body: str) -> str:
    """Drop the reply fallback: the leading run of `>` lines and one blank line after it."""
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    if i == 0:
        return body
    if i < len(lines) and lines[i] == "":
        i += 1
    return "\n".join(lines[i:])


def _new_body(event: object) -> str | None:
    if not isinstance(event, dict):
        return None
    new = _content(event).get("m.new_content")
    body = new.get("body") if isinstance(new, dict) else None
    return body if isinstance(body, str) else None


def bundled_edit(event: dict, sender: str) -> tuple[str, int] | None:
    """The latest edit the server bundled into the event (`unsigned.m.relations.m.replace`),
    only when the original sender made it: (new body, origin_server_ts)."""
    unsigned = event.get("unsigned")
    relations = unsigned.get("m.relations") if isinstance(unsigned, dict) else None
    replace = relations.get("m.replace") if isinstance(relations, dict) else None
    body = _new_body(replace)
    if body is None or replace.get("sender") != sender:
        return None
    ts = _int(replace.get("origin_server_ts"))
    return (body, ts) if ts is not None else None


def parse_edit(
    room_id: str, event: dict, self_ids: frozenset[str], bridge_bots: Sequence[str]
) -> Edit | None:
    sender = str(event.get("sender", ""))
    if event.get("type") not in MESSAGE_TYPES or sender in self_ids or sender in bridge_bots:
        return None
    relates = _content(event).get("m.relates_to")
    if not isinstance(relates, dict) or relates.get("rel_type") != "m.replace":
        return None
    target = relates.get("event_id")
    body = _new_body(event)
    if not isinstance(target, str) or body is None:
        return None
    return Edit(
        event_id=str(event.get("event_id", "")),
        room_id=room_id,
        sender=sender,
        target=target,
        body=body,
        origin_ts=int(event.get("origin_server_ts") or 0),
    )


def parse_redaction(event: dict) -> Redaction | None:
    if event.get("type") != "m.room.redaction":
        return None
    target = _content(event).get("redacts")
    if not isinstance(target, str):
        target = event.get("redacts")
    if not isinstance(target, str):
        return None
    return Redaction(str(event.get("event_id", "")), target)


def parse_message(
    room_id: str, event: dict, self_ids: frozenset[str], bridge_bots: Sequence[str]
) -> Candidate | Skip | Unsupported | Redacted:
    event_id = str(event.get("event_id", ""))
    sender = str(event.get("sender", ""))
    skip = Skip(event_id, sender)
    event_type = event.get("type")
    if event_type not in MESSAGE_TYPES:
        return skip
    if sender in self_ids or sender in bridge_bots:
        return skip
    content = _content(event)
    relates = content.get("m.relates_to")
    if isinstance(relates, dict) and relates.get("rel_type") == "m.replace":
        return skip
    body = content.get("body")
    if not isinstance(body, str):
        unsigned = event.get("unsigned")
        if isinstance(unsigned, dict) and isinstance(unsigned.get("redacted_because"), dict):
            return Redacted(event_id, sender)
        return skip
    reply_to: str | None = None
    reply = relates.get("m.in_reply_to") if isinstance(relates, dict) else None
    if isinstance(reply, dict):
        if isinstance(reply.get("event_id"), str):
            reply_to = reply["event_id"]
        body = strip_reply_fallback(body)
    msgtype = content.get("msgtype")
    media: Media | None = None
    if event_type == "m.sticker" or msgtype in _MEDIA_MSGTYPES:
        kind = "sticker" if event_type == "m.sticker" else _MEDIA_MSGTYPES[msgtype]
        parsed = _media(content, kind, body)
        if parsed is None:
            return Unsupported(event_id, sender)
        body, media = parsed
    elif msgtype not in _TEXT_MSGTYPES:
        return Unsupported(event_id, sender)
    formatted = content.get("formatted_body")
    mentions = content.get("m.mentions")
    ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
    edited = bundled_edit(event, sender)
    return Candidate(
        event_id=event_id,
        room_id=room_id,
        sender=sender,
        body=body,
        formatted_body=formatted if isinstance(formatted, str) else None,
        mention_ids=tuple(i for i in ids if isinstance(i, str)) if isinstance(ids, list) else (),
        origin_ts=int(event.get("origin_server_ts") or 0),
        media=media,
        reply_to=reply_to,
        edited_body=edited[0] if edited else None,
        edited_ts=edited[1] if edited else None,
    )


@dataclass(frozen=True)
class ApprovalEvent:
    """An approver's reaction (`reaction_key`) or quoted reply (`text`) on `target_event_id`
    in the approvals room; whether it is a verdict on an approval is decided later."""

    event_id: str
    sender: str
    target_event_id: str
    reaction_key: str | None
    text: str | None


def parse_approval_event(
    room_id: str, event: dict, approvals_room: str, approver_ids: frozenset[str]
) -> ApprovalEvent | None:
    """None unless the event is in the approvals room, sent by an approver, and is either an
    `m.annotation` reaction or an `m.text` reply (`m.in_reply_to`) that is not an edit."""
    sender = event.get("sender")
    if room_id != approvals_room or sender not in approver_ids:
        return None
    event_id = str(event.get("event_id", ""))
    content = _content(event)
    relates = content.get("m.relates_to")
    if not isinstance(relates, dict):
        return None
    if event.get("type") == "m.reaction":
        target, key = relates.get("event_id"), relates.get("key")
        if relates.get("rel_type") != "m.annotation" or not isinstance(target, str):
            return None
        return ApprovalEvent(event_id, sender, target, key, None) if isinstance(key, str) else None
    if event.get("type") != "m.room.message" or content.get("msgtype") != "m.text":
        return None
    if relates.get("rel_type") == "m.replace":
        return None
    reply = relates.get("m.in_reply_to")
    target = reply.get("event_id") if isinstance(reply, dict) else None
    body = content.get("body")
    if not isinstance(target, str) or not isinstance(body, str):
        return None
    return ApprovalEvent(event_id, sender, target, None, body)


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
