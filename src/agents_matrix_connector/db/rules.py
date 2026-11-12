"""Access rules, room settings and the default preset.

Written only by the API; the sync loop and the dispatch worker read. Readers accept either a
connection (so a caller can read inside its own transaction) or the pool.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
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
    preamble: str | None
    created_at: datetime


@dataclass(frozen=True)
class RoomSettings:
    preset_id: uuid.UUID | None
    trigger: str | None
    agents_may_initiate: bool = False
    anonymize_names: bool = False
    agent_aliases: tuple[str, ...] = ()


@asynccontextmanager
async def _conn(source: Source) -> AsyncIterator[psycopg.AsyncConnection]:
    if isinstance(source, AsyncConnectionPool):
        async with source.connection() as conn:
            yield conn
    else:
        yield source


async def add_rule(
    pool: AsyncConnectionPool,
    list_: str,
    kind: str,
    value: str,
    note: str | None,
    preamble: str | None = None,
) -> policy.Rule:
    list_, kind, value = policy.validate_rule(list_, kind, value)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO access_rules (list, kind, value, note, preamble)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (list_, kind, value, note, preamble),
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
            "SELECT id, list, kind, value, note, preamble, created_at FROM access_rules ORDER BY id"
        )
        return [RuleRow(**row) for row in await cur.fetchall()]


async def get_room_settings(source: Source, room_id: str) -> RoomSettings | None:
    async with _conn(source) as conn:
        cur = await conn.execute(
            "SELECT preset_id, trigger, agents_may_initiate, anonymize_names, agent_aliases"
            " FROM room_settings WHERE room_id = %s",
            (room_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return RoomSettings(*row[:4], tuple(row[4]))


async def set_room_settings(
    pool: AsyncConnectionPool,
    room_id: str,
    *,
    preset_id: uuid.UUID | None,
    trigger: str | None,
    agents_may_initiate: bool | None = None,
    anonymize_names: bool | None = None,
    agent_aliases: Sequence[str] | None = None,
) -> RoomSettings:
    """Store the room's preset and trigger; a flag given as None keeps its stored value.

    A new row without a flag gets false. `agents_may_initiate` is an authorisation grant and
    `anonymize_names` a privacy switch, so a save that does not name one never changes it.
    `agent_aliases` given as None keeps the stored list, a list (even empty) replaces it.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO room_settings (room_id, preset_id, trigger, agents_may_initiate,
                                       anonymize_names, agent_aliases)
            VALUES (%(room)s, %(preset)s, %(trigger)s, COALESCE(%(flag)s::boolean, false),
                    COALESCE(%(anon)s::boolean, false),
                    COALESCE(%(aliases)s::text[], '{}'))
            ON CONFLICT (room_id) DO UPDATE
            SET preset_id = EXCLUDED.preset_id, trigger = EXCLUDED.trigger,
                agents_may_initiate = COALESCE(%(flag)s::boolean,
                                               room_settings.agents_may_initiate),
                anonymize_names = COALESCE(%(anon)s::boolean, room_settings.anonymize_names),
                agent_aliases = COALESCE(%(aliases)s::text[], room_settings.agent_aliases),
                updated_at = now()
            RETURNING agents_may_initiate, anonymize_names, agent_aliases
            """,
            {
                "room": room_id,
                "preset": preset_id,
                "trigger": trigger,
                "flag": agents_may_initiate,
                "anon": anonymize_names,
                "aliases": None if agent_aliases is None else list(agent_aliases),
            },
        )
        flag, anon, aliases = await cur.fetchone()
    return RoomSettings(preset_id, trigger, flag, anon, tuple(aliases))


async def allow_room(
    conn: psycopg.AsyncConnection,
    room_id: str,
    *,
    preset_id: uuid.UUID | None,
    trigger: str | None,
    agents_may_initiate: bool | None,
    note: str,
    agent_aliases: Sequence[str] | None = None,
) -> int:
    """In the caller's transaction: store the room settings (as `set_room_settings`) and the
    `allow/room` rule for the room id, reusing an existing one. Returns the rule id."""
    await conn.execute(
        """
        INSERT INTO room_settings (room_id, preset_id, trigger, agents_may_initiate,
                                   agent_aliases)
        VALUES (%(room)s, %(preset)s, %(trigger)s, COALESCE(%(flag)s::boolean, false),
                COALESCE(%(aliases)s::text[], '{}'))
        ON CONFLICT (room_id) DO UPDATE
        SET preset_id = EXCLUDED.preset_id, trigger = EXCLUDED.trigger,
            agents_may_initiate = COALESCE(%(flag)s::boolean, room_settings.agents_may_initiate),
            agent_aliases = COALESCE(%(aliases)s::text[], room_settings.agent_aliases),
            updated_at = now()
        """,
        {
            "room": room_id,
            "preset": preset_id,
            "trigger": trigger,
            "flag": agents_may_initiate,
            "aliases": None if agent_aliases is None else list(agent_aliases),
        },
    )
    list_, kind, value = policy.validate_rule("allow", "room", room_id)
    cur = await conn.execute(
        "INSERT INTO access_rules (list, kind, value, note) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING RETURNING id",
        (list_, kind, value, note),
    )
    row = await cur.fetchone()
    if row is not None:
        return row[0]
    cur = await conn.execute(
        "SELECT id FROM access_rules WHERE list = %s AND kind = %s AND value = %s",
        (list_, kind, value),
    )
    return (await cur.fetchone())[0]


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


async def set_rule_preamble(pool: AsyncConnectionPool, rule_id: int, preamble: str | None) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE access_rules SET preamble = %s WHERE id = %s", (preamble, rule_id)
        )
        return cur.rowcount == 1


async def get_rule_preamble(source: Source, rule_id: int) -> tuple[str | None, str | None] | None:
    """`(note, preamble)` of the rule, None when it no longer exists."""
    async with _conn(source) as conn:
        cur = await conn.execute(
            "SELECT note, preamble FROM access_rules WHERE id = %s", (rule_id,)
        )
        row = await cur.fetchone()
    return None if row is None else (row[0], row[1])


async def get_preamble(source: Source) -> str | None:
    async with _conn(source) as conn:
        cur = await conn.execute("SELECT preamble FROM settings WHERE id = 1")
        return (await cur.fetchone())[0]


async def set_preamble(pool: AsyncConnectionPool, preamble: str | None) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE settings SET preamble = %s, updated_at = now() WHERE id = 1", (preamble,)
        )
