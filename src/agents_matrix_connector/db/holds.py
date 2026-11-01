"""Room holds: a room the worker leaves alone until a time (a refused usage quota).

The bot's dispatch worker is the only writer; `inbound.rooms_ready` reads the holds. The
caller owns the transaction.
"""

from __future__ import annotations

from datetime import datetime

import psycopg


async def hold(conn: psycopg.AsyncConnection, room_id: str, until: datetime, reason: str) -> None:
    """Hold the room until `until`; a room that is already held gets the new time and reason."""
    await conn.execute(
        """
        INSERT INTO room_holds (room_id, until, reason) VALUES (%s, %s, %s)
        ON CONFLICT (room_id)
        DO UPDATE SET until = EXCLUDED.until, reason = EXCLUDED.reason, updated_at = now()
        """,
        (room_id, until, reason),
    )
