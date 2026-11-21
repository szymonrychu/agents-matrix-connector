"""Per-room notes in named sections (writer: api; the bot worker only reads them).

`SECTIONS` is the fixed table of sections, in display order. One row per (room, section) with
an optimistic version: `put_section` with `expected_version == 0` creates, any other value is
one status-guarded UPDATE on the version. The caller owns the transaction. Bodies are readable
data, but callers still never log them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

import psycopg
from psycopg.rows import dict_row

SectionKey = Literal[
    "preferences",
    "requests",
    "work-in-progress",
    "provided-information",
    "repeatable-workflows",
]


@dataclass(frozen=True)
class Section:
    key: SectionKey
    title: str
    inject: bool
    """Shown in the first prompt of a session; the others appear there only as an index line."""


SECTIONS: tuple[Section, ...] = (
    Section("preferences", "Preferences", True),
    Section("requests", "Requests", True),
    Section("work-in-progress", "Work in progress", True),
    Section("provided-information", "Provided information", False),
    Section("repeatable-workflows", "Repeatable workflows", False),
)
assert tuple(s.key for s in SECTIONS) == get_args(SectionKey)

_COLUMNS = "room_id, section, body, version, updated_at, updated_by_slug"


@dataclass(frozen=True)
class SectionRow:
    room_id: str
    section: SectionKey
    body: str
    version: int
    updated_at: datetime
    updated_by_slug: str | None


class SectionTooLarge(Exception):
    def __init__(self, section: str, cap: int, got: int):
        super().__init__(section, cap, got)
        self.section, self.cap, self.got = section, cap, got


class NotesTooLarge(Exception):
    """All sections of the room together would exceed the total cap."""

    def __init__(self, cap: int, got: int):
        super().__init__(cap, got)
        self.cap, self.got = cap, got


class VersionConflict(Exception):
    def __init__(self, expected: int, current: int):
        super().__init__(expected, current)
        self.expected, self.current = expected, current


def _room_lock_key(room_id: str) -> int:
    digest = hashlib.sha256(f"room_note_sections:{room_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def get_all(conn: psycopg.AsyncConnection, room_id: str) -> list[SectionRow]:
    """The stored sections of the room (sections never written are absent)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM room_note_sections WHERE room_id = %s", (room_id,)
        )
        rows = await cur.fetchall()
    return [SectionRow(**row) for row in rows]


async def put_section(
    conn: psycopg.AsyncConnection,
    room_id: str,
    section: SectionKey,
    body: str,
    expected_version: int,
    slug: str | None,
    *,
    section_cap: int,
    total_cap: int,
) -> SectionRow:
    """Replace one section's body. Raises `SectionTooLarge`, `NotesTooLarge` or
    `VersionConflict`. Must run inside a transaction: a per-room advisory lock serialises
    writers before the other sections' sizes are read, so two concurrent writes to different
    sections cannot together pass the total cap."""
    if len(body) > section_cap:
        raise SectionTooLarge(section, section_cap, len(body))
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_room_lock_key(room_id),))
        await cur.execute(
            "SELECT coalesce(sum(char_length(body)), 0) AS chars FROM room_note_sections"
            " WHERE room_id = %s AND section <> %s",
            (room_id, section),
        )
        others = (await cur.fetchone())["chars"]
        if others + len(body) > total_cap:
            raise NotesTooLarge(total_cap, others + len(body))
        if expected_version == 0:
            await cur.execute(
                f"INSERT INTO room_note_sections (room_id, section, body, version,"
                f" updated_by_slug) VALUES (%s, %s, %s, 1, %s)"
                f" ON CONFLICT (room_id, section) DO NOTHING RETURNING {_COLUMNS}",
                (room_id, section, body, slug),
            )
        else:
            await cur.execute(
                f"UPDATE room_note_sections SET body = %s, version = version + 1,"
                f" updated_at = now(), updated_by_slug = %s"
                f" WHERE room_id = %s AND section = %s AND version = %s RETURNING {_COLUMNS}",
                (body, slug, room_id, section, expected_version),
            )
        row = await cur.fetchone()
        if row is None:
            await cur.execute(
                "SELECT version FROM room_note_sections WHERE room_id = %s AND section = %s",
                (room_id, section),
            )
            current = await cur.fetchone()
            raise VersionConflict(expected_version, current["version"] if current else 0)
    return SectionRow(**row)
