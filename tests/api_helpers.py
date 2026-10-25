"""Constants and helpers shared by the API tests."""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from agents_matrix_connector.settings import ConnectorSettings

PACKAGE = "agents_matrix_connector"
PREFIX = "/matrix/api/v1"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

DM = "!dm:x"
GRP = "!grp:x"
ENC = "!enc:x"
INFRA = "!infra:x"
ALICE = "@facebook_111:x"
BOB = "@facebook_222:x"
BOT = "@agents:x"


def make_settings(**extra) -> ConnectorSettings:
    return ConnectorSettings(
        database_url="postgresql://u:p@db/x",
        orchestrator_url="http://orch",
        orchestrator_token="tok",
        homeserver_url="http://hs:8008",
        user_id=BOT,
        sync_timeout_seconds=30,
        **extra,
    )


async def seed(conn):
    for room_id, kind, name, alias, encrypted, members, last in [
        (DM, "dm", "Alice", None, False, 2, T0 + timedelta(minutes=3)),
        (GRP, "group", "Team", "#team:x", False, 3, T0 + timedelta(minutes=2)),
        (ENC, "group", "Secret", None, True, 3, T0 + timedelta(minutes=1)),
        (INFRA, "infrastructure", "Space", None, False, 1, None),
    ]:
        await conn.execute(
            "INSERT INTO rooms (room_id, kind, name, alias, encrypted, membership, member_count,"
            " last_event_at) VALUES (%s, %s, %s, %s, %s, 'join', %s, %s)",
            (room_id, kind, name, alias, encrypted, members, last),
        )
    for user_id, name, fb in [
        (ALICE, "Alice A", "111"),
        (BOB, "Bob B", "222"),
        (BOT, "Agents", None),
    ]:
        await conn.execute(
            "INSERT INTO users (user_id, display_name, facebook_id) VALUES (%s, %s, %s)",
            (user_id, name, fb),
        )
    for room_id, user_id in [(DM, ALICE), (DM, BOB), (GRP, ALICE), (GRP, BOT)]:
        await conn.execute(
            "INSERT INTO room_members (room_id, user_id, membership) VALUES (%s, %s, 'join')",
            (room_id, user_id),
        )
    await conn.execute(
        "INSERT INTO access_rules (list, kind, value) VALUES ('deny', 'user', %s)", (BOB,)
    )
    await conn.execute(
        "INSERT INTO access_rules (list, kind, value) VALUES ('allow', 'room', %s)", (DM,)
    )


async def add_inbound(conn, n, room=DM, status="handled", run_id=None, at=None):
    await conn.execute(
        "INSERT INTO inbound (event_id, room_id, sender, sender_name, body, status, run_id,"
        " received_at) VALUES (%s, %s, %s, 'Alice A', %s, %s, %s, %s)",
        (
            f"$e{n}{room}",
            room,
            ALICE,
            f"hello {n}",
            status,
            run_id,
            at or T0 + timedelta(seconds=n),
        ),
    )


async def add_outbound(conn, n, room=DM, run_id=None, at=None):
    await conn.execute(
        "INSERT INTO outbound (room_id, run_id, kind, body, txn_id, status, created_at)"
        " VALUES (%s, %s, 'reply', %s, %s, 'sent', %s)",
        (room, run_id, f"answer {n}", f"txn-{n}", at or T0 + timedelta(seconds=n)),
    )


def room_path(room_id: str, suffix: str = "") -> str:
    return f"/rooms/{quote(room_id, safe='')}{suffix}"
