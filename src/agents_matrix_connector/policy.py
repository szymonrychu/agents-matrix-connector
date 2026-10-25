"""Access policy: rule validation and the allow/deny decision (design D4, D6).

Pure functions, no I/O. Identifiers are compared exactly; only display names
are compared case-insensitively, and they can only deny.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

KINDS: dict[str, tuple[str, ...]] = {
    "allow": ("user", "facebook", "room", "alias"),
    "deny": ("user", "facebook", "room", "alias", "display_name"),
}

ROOM_INFRASTRUCTURE = "room_infrastructure"
ROOM_ENCRYPTED = "room_encrypted"
NOT_ALLOWED = "not_allowed"

DEFAULT_POLICIES = ("allow", "deny")

MAX_VALUE_CHARS = 255
MAX_DISPLAY_NAME_CHARS = 128

_FORMATS: dict[str, tuple[re.Pattern[str], str]] = {
    "user": (re.compile(r"@[^:\s]+:\S+"), "a Matrix user id like @local:server"),
    "facebook": (re.compile(r"[0-9]{1,32}"), "1 to 32 digits"),
    "room": (re.compile(r"![^:\s]+:\S+"), "a Matrix room id like !id:server"),
    "alias": (re.compile(r"#[^:\s]+:\S+"), "a Matrix room alias like #alias:server"),
}

_ROOM_KINDS = ("room", "alias")
_SENDER_KINDS = ("user", "facebook")


def denied(kind: str) -> str:
    return f"denied: {kind}"


@dataclass(frozen=True)
class Rule:
    id: int
    list: str
    kind: str
    value: str


@dataclass(frozen=True)
class Subject:
    room_id: str
    room_alias: str | None
    room_kind: str | None
    encrypted: bool
    sender_id: str | None
    sender_facebook_id: str | None
    sender_name: str | None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None
    rule_id: int | None
    source: Literal["room", "rule", "default"]


def validate_rule(list_: str, kind: str, value: str) -> tuple[str, str, str]:
    """Return the canonical (list, kind, value) or raise ValueError naming the field first."""
    if list_ not in KINDS:
        raise ValueError(f"list: must be one of {', '.join(KINDS)}")
    if list_ == "allow" and kind == "display_name":
        raise ValueError(
            "kind: display_name cannot be an allow rule; display names are chosen by the "
            "sender and cannot grant access"
        )
    if kind not in KINDS[list_]:
        raise ValueError(f"kind: must be one of {', '.join(KINDS[list_])} for {list_}")
    value = value.strip()
    if not 1 <= len(value) <= MAX_VALUE_CHARS:
        raise ValueError(f"value: must be 1 to {MAX_VALUE_CHARS} characters")
    if kind == "display_name":
        if len(value) > MAX_DISPLAY_NAME_CHARS:
            raise ValueError(f"value: must be 1 to {MAX_DISPLAY_NAME_CHARS} characters")
    else:
        pattern, expected = _FORMATS[kind]
        if pattern.fullmatch(value) is None:
            raise ValueError(f"value: {kind} must be {expected}")
    return list_, kind, value


def _matches(rule: Rule, subject: Subject) -> bool:
    if rule.kind == "room":
        return rule.value == subject.room_id
    if rule.kind == "alias":
        return subject.room_alias is not None and rule.value == subject.room_alias
    if subject.sender_id is None:
        return False
    if rule.kind == "user":
        return rule.value == subject.sender_id
    if rule.kind == "facebook":
        return subject.sender_facebook_id is not None and rule.value == subject.sender_facebook_id
    if rule.kind == "display_name":
        return (
            subject.sender_name is not None
            and rule.value.casefold() == subject.sender_name.casefold()
        )
    return False


def _first(
    rules: Sequence[Rule], list_: str, kinds: tuple[str, ...], subject: Subject
) -> Rule | None:
    for rule in rules:
        if rule.list == list_ and rule.kind in kinds and _matches(rule, subject):
            return rule
    return None


def evaluate(subject: Subject, rules: Sequence[Rule], default_policy: str) -> Decision:
    """Decide a message (or a room, when sender_id is None); the first hit wins."""
    if default_policy not in DEFAULT_POLICIES:
        raise ValueError(f"default_policy: must be one of {', '.join(DEFAULT_POLICIES)}")
    if subject.room_kind == "infrastructure":
        return Decision(False, ROOM_INFRASTRUCTURE, None, "room")
    if subject.encrypted:
        return Decision(False, ROOM_ENCRYPTED, None, "room")
    ordered = sorted(rules, key=lambda rule: rule.id)
    deny = _first(ordered, "deny", KINDS["deny"], subject)
    if deny is not None:
        return Decision(False, denied(deny.kind), deny.id, "rule")
    allow = _first(ordered, "allow", _ROOM_KINDS, subject) or _first(
        ordered, "allow", _SENDER_KINDS, subject
    )
    if allow is not None:
        return Decision(True, None, allow.id, "rule")
    if default_policy == "allow":
        return Decision(True, None, None, "default")
    return Decision(False, NOT_ALLOWED, None, "default")
