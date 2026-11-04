"""Pseudonyms of rooms flagged `anonymize_names` and the outbound pseudonym check (pure).

A person is `user-<sha>` (or `admin-<sha>` when listed in `MATRIX_ANONYMIZE_ADMINS`), `<sha>`
being the first 12 hex characters of a plain sha256 of the Matrix user id. The chat agent sees
only pseudonyms in identity fields (sender, room name); message bodies are never rewritten and
real names may be sent. A pseudonym must never reach the chat: outbound text containing one is
rejected (`pseudonym_disclosed`). Text is compared in a folded form: NFKC, casefold, NFKC
again, with default-ignorable code points (zero-width joiners and the like) dropped, matching
only whole words; every match maps back to its span in the original text.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Protocol

PSEUDONYM_DISCLOSED = "pseudonym_disclosed"
DIGEST_CHARS = 12
_PSEUDONYM_RE = re.compile(rf"(?<![a-z0-9])(?:user|admin)-[0-9a-f]{{{DIGEST_CHARS}}}(?![g-z])")
"""A pseudonym token in folded (lowercase) text."""

_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xE0000, 0xE0FFF),
)


class Sent(Protocol):
    @property
    def sender(self) -> str: ...


class Member(Protocol):
    @property
    def user_id(self) -> str: ...

    @property
    def display_name(self) -> str | None: ...


def pseudonym(user_id: str, *, admin: bool = False) -> str:
    digest = hashlib.sha256(user_id.encode()).hexdigest()[:DIGEST_CHARS]
    return f"{'admin' if admin else 'user'}-{digest}"


def no_facebook_id(_user_id: str) -> str | None:
    """The Facebook id an agent sees of anyone in an anonymized room: none."""
    return None


def is_admin(user_id: str, facebook_id: str | None, admins: Collection[str]) -> bool:
    """Whether the person is listed by Matrix user id or by numeric Facebook id."""
    return user_id in admins or (facebook_id is not None and facebook_id in admins)


def _ignorable(ch: str) -> bool:
    if unicodedata.category(ch) == "Cf":
        return True
    code = ord(ch)
    return any(low <= code <= high for low, high in _IGNORABLE_RANGES)


def _fold(text: str) -> tuple[str, list[int], list[int]]:
    """The folded text plus, per folded character, the start and end of its source span.

    Each base character is folded with the combining marks that follow it, so a decomposed
    accent composes like the precomposed one."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i = 0
    while i < len(text):
        j = i + 1
        while j < len(text) and unicodedata.combining(text[j]):
            j += 1
        nfkc = unicodedata.normalize("NFKC", text[i:j])
        for ch in unicodedata.normalize("NFKC", nfkc.casefold()):
            if not _ignorable(ch):
                out.append(ch)
                starts.append(i)
                ends.append(j)
        i = j
    return "".join(out), starts, ends


def _name_pattern(name: str) -> re.Pattern[str] | None:
    parts = _fold(name)[0].split()
    if not parts:
        return None
    return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, parts)) + r"(?!\w)")


def pseudonyms_in(text: str) -> list[str]:
    """The pseudonym tokens in `text` as first written there, each token once (compared
    folded), in order of appearance."""
    folded, starts, ends = _fold(text)
    found: dict[str, str] = {}
    for match in _PSEUDONYM_RE.finditer(folded):
        found.setdefault(match.group(), text[starts[match.start()] : ends[match.end() - 1]])
    return list(found.values())


def disclosure_message(tokens: Iterable[str]) -> str:
    """Why a text with pseudonyms is not sent, for the agent that wrote it."""
    return (
        f"contains the pseudonym(s) {', '.join(tokens)}; pseudonyms (user-<sha>, admin-<sha>) "
        "must not be sent to the chat, refer to the person differently (for example by the "
        "name the chat users gave, or without naming them)"
    )


def _spans(folded: str, starts: list[int], ends: list[int], pattern: re.Pattern[str], label: str):
    for match in pattern.finditer(folded):
        if match.end() > match.start():
            yield starts[match.start()], ends[match.end() - 1], label


def _apply(text: str, labelled: Iterable[tuple[re.Pattern[str], str]]) -> str:
    folded, starts, ends = _fold(text)
    spans = [s for pattern, label in labelled for s in _spans(folded, starts, ends, pattern, label)]
    spans.sort(key=lambda s: (s[0], s[0] - s[1]))
    out = []
    at = 0
    for start, end, label in spans:
        if start < at:
            continue
        out.append(text[at:start])
        out.append(label)
        at = end
    out.append(text[at:])
    return "".join(out)


def replace(text: str, labels: Mapping[str, str]) -> str:
    """Every whole occurrence of a name replaced by its label, the longest match first."""
    labelled = [(p, label) for name, label in labels.items() if (p := _name_pattern(name))]
    return _apply(text, labelled)


class Labeler:
    """The pseudonyms of one deployment: `admins` lists Matrix user ids or Facebook ids."""

    def __init__(self, admins: Collection[str], facebook_id: Callable[[str], str | None]):
        self._admins = frozenset(admins)
        self._facebook_id = facebook_id

    def user(self, user_id: str) -> str:
        admin = is_admin(user_id, self._facebook_id(user_id), self._admins)
        return pseudonym(user_id, admin=admin)

    def sender(self, row: Sent) -> str:
        """The pseudonym of a message's sender."""
        return self.user(row.sender)

    def room(
        self,
        name: str | None,
        kind: str | None,
        members: Iterable[Member],
        excluded: Collection[str],
    ) -> str | None:
        """The room name an agent sees: a DM is named after its people's pseudonyms (its name
        is the other person's name), any other room keeps its name with member names replaced."""
        people = [m for m in members if m.user_id not in excluded]
        if kind == "dm":
            return ", ".join(self.user(m.user_id) for m in people) or None
        if name is None:
            return None
        labels = {m.display_name: self.user(m.user_id) for m in people if m.display_name}
        return replace(name, labels)
