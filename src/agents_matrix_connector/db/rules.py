"""Access rules, room settings and the default preset.

Written only by the API; the sync loop and the dispatch worker read. Readers accept either a
connection (so a caller can read inside its own transaction) or the pool.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import policy

Source = psycopg.AsyncConnection | AsyncConnectionPool


class RuleExists(Exception):  # noqa: N818
    """The (list, kind, value) rule is already stored."""


@dataclass(frozen=True)
class RuleRow:
    id: int
    list: str
    kind: str
    value: str
    note: str | None
    created_at: datetime


@dataclass(frozen=True)
class RoomSettings:
    preset_id: uuid.UUID | None
    trigger: str | None
    agents_may_initiate: bool = False


@asynccontextmanager
async def _conn(source: Source) -> AsyncIterator[psycopg.AsyncConnection]:
    if isinstance(source, AsyncConnectionPool):
        async with source.connection() as conn:
            yield conn
    else:
        yield source


async def add_rule(
    pool: AsyncConnectionPool, list_: str, kind: str, value: str, note: str | None
) -> policy.Rule:
    list_, kind, value = policy.validate_rule(list_, kind, value)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO access_rules (list, kind, value, note) VALUES (%s, %s, %s, %s)"
                " RETURNING id",
                (list_, kind, value, note),
            )
            rule_id = (await cur.fetchone())[0]
    except errors.UniqueViolation as exc:
        raise RuleExists(f"{list_} {kind} rule {value!r} already exists") from exc
    return policy.Rule(rule_id, list_, kind, value)


async def delete_rule(pool: AsyncConnectionPool, rule_id: int) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM access_rules WHERE id = %s", (rule_id,))
        return cur.rowcount == 1


async def list_rules(source: Source) -> list[policy.Rule]:
    async with _conn(source) as conn:
        cur = await conn.execute("SELECT id, list, kind, value FROM access_rules ORDER BY id")
        return [policy.Rule(*row) for row in await cur.fetchall()]


async def list_rule_rows(pool: AsyncConnectionPool) -> list[RuleRow]:
    async with pool.connection() as conn:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT id, list, kind, value, note, created_at FROM access_rules ORDER BY id"
        )
        return [RuleRow(**row) for row in await cur.fetchall()]


async def get_room_settings(source: Source, room_id: str) -> RoomSettings | None:
    async with _conn(source) as conn:
        cur = await conn.execute(
            "SELECT preset_id, trigger, agents_may_initiate FROM room_settings WHERE room_id = %s",
            (room_id,),
        )
        row = await cur.fetchone()
    return None if row is None else RoomSettings(*row)


async def set_room_settings(
    pool: AsyncConnectionPool,
    room_id: str,
    *,
    preset_id: uuid.UUID | None,
    trigger: str | None,
    agents_may_initiate: bool | None = None,
) -> RoomSettings:
    """Store the room's preset and trigger; `agents_may_initiate` None keeps the stored flag.

    A new row without the flag gets false. The flag is an authorisation grant, so a save that
    does not name it never grants or revokes it.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO room_settings (room_id, preset_id, trigger, agents_may_initiate)
            VALUES (%(room)s, %(preset)s, %(trigger)s, COALESCE(%(flag)s::boolean, false))
            ON CONFLICT (room_id) DO UPDATE
            SET preset_id = EXCLUDED.preset_id, trigger = EXCLUDED.trigger,
                agents_may_initiate = COALESCE(%(flag)s::boolean,
                                               room_settings.agents_may_initiate),
                updated_at = now()
            RETURNING agents_may_initiate
            """,
            {"room": room_id, "preset": preset_id, "trigger": trigger, "flag": agents_may_initiate},
        )
        stored = (await cur.fetchone())[0]
    return RoomSettings(preset_id, trigger, stored)


async def get_default_preset(source: Source) -> uuid.UUID | None:
    async with _conn(source) as conn:
        cur = await conn.execute("SELECT default_preset_id FROM settings WHERE id = 1")
        return (await cur.fetchone())[0]


async def set_default_preset(pool: AsyncConnectionPool, preset_id: uuid.UUID | None) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE settings SET default_preset_id = %s, updated_at = now() WHERE id = 1",
            (preset_id,),
        )
