"""The agent routes under `/chat/v1` (design D2, D4, D5, D6, D9).

The api only inserts `sends` rows and never writes `room_bindings`; tests seed bindings, runs,
inbound rows and send statuses with plain SQL, as the bot worker would.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from datetime import datetime

import httpx
import pytest
from api_helpers import BOT, DM, GRP, make_settings
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector.anonymize import pseudonym
from agents_matrix_connector.api import agent as agent_api
from agents_matrix_connector.api.agent import CHAT_PREFIX, create_agent_app, valid_key
from agents_matrix_connector.db.sends import parse_ref
from agents_matrix_connector.prompt import MEMORY_RULE

TOKEN = secrets.token_hex(32)
DIGEST = hashlib.sha256(TOKEN.encode()).hexdigest()

ALICE = "@facebook_111:x"
BOB = "@facebook_222:x"
CAROL = "@facebook_333:x"
FACEBOOK_IDS = {ALICE: "111", BOB: "222", CAROL: "333", BOT: None}


# --- seeding -----------------------------------------------------------------------------------


async def add_room(conn, room_id, *, kind="dm", name=None, members=(ALICE,), flag=None, enc=False):
    await conn.execute(
        "INSERT INTO rooms (room_id, kind, name, encrypted, membership, member_count)"
        " VALUES (%s, %s, %s, %s, 'join', %s)",
        (room_id, kind, name, enc, len(members)),
    )
    for user_id in members:
        await conn.execute(
            "INSERT INTO users (user_id, facebook_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, FACEBOOK_IDS[user_id]),
        )
        await conn.execute(
            "INSERT INTO room_members (room_id, user_id, membership) VALUES (%s, %s, 'join')",
            (room_id, user_id),
        )
    if flag is not None:
        await conn.execute(
            "INSERT INTO room_settings (room_id, agents_may_initiate) VALUES (%s, %s)",
            (room_id, flag),
        )


async def add_rule(conn, list_, kind, value):
    await conn.execute(
        "INSERT INTO access_rules (list, kind, value) VALUES (%s, %s, %s)", (list_, kind, value)
    )


async def allow_room(conn, room_id):
    await add_rule(conn, "allow", "room", room_id)


async def bind(conn, room_id, session_id, slug="hive-1"):
    await conn.execute(
        "INSERT INTO room_bindings (room_id, session_id, slug, origin, send_id)"
        " VALUES (%s, %s, %s, 'agent', 1)",
        (room_id, session_id, slug),
    )


async def reserve(conn, room_id):
    """A first run in progress that has no session yet reserves the room."""
    cur = await conn.execute(
        "INSERT INTO runs (room_id, preset_id, kind, slug, status)"
        " VALUES (%s, %s, 'first', 'mx-1', 'in-progress') RETURNING id",
        (room_id, uuid.uuid4()),
    )
    run_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO room_bindings (room_id, origin, run_id) VALUES (%s, 'inbound', %s)",
        (room_id, run_id),
    )


async def add_inbound(conn, room_id, *, status="handled", reason=None) -> tuple[str, str]:
    """An inbound row; returns its `m<n>` and its event id."""
    event_id = f"$in-{secrets.token_hex(4)}"
    cur = await conn.execute(
        "INSERT INTO inbound (event_id, room_id, sender, body, status, reason)"
        " VALUES (%s, %s, %s, 'hello', %s, %s) RETURNING id",
        (event_id, room_id, ALICE, status, reason),
    )
    return f"m{(await cur.fetchone())[0]}", event_id


async def add_send(
    conn,
    session_id,
    room_id,
    *,
    kind="message",
    text="seeded",
    key=None,
    reply_to=None,
    status="sent",
    event_id=None,
) -> str:
    """A send row as the api inserted and the bot moved it; returns its `s<n>`."""
    cur = await conn.execute(
        "INSERT INTO sends (session_id, slug, kind, room_id, text, key, reply_to, event_id,"
        " status) VALUES (%s, 'hive-1', %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            session_id,
            kind,
            room_id,
            text if kind == "message" else None,
            key if kind == "reaction" else None,
            reply_to,
            event_id or (f"$out-{secrets.token_hex(4)}" if status == "sent" else None),
            status,
        ),
    )
    return f"s{(await cur.fetchone())[0]}"


async def set_send(conn, send_ref, **fields):
    sets = ", ".join(f"{name} = %s" for name in fields)
    await conn.execute(
        f"UPDATE sends SET {sets} WHERE id = %s", (*fields.values(), int(send_ref[1:]))
    )


async def age_sends(conn, session_id, seconds=61):
    await conn.execute(
        "UPDATE sends SET created_at = now() - make_interval(secs => %s) WHERE session_id = %s",
        (seconds, session_id),
    )


async def send_row(conn, send_ref) -> dict:
    cur = await conn.execute(
        "SELECT session_id, slug, kind, room_id, text, key, reply_to, ref_event_id, status"
        " FROM sends WHERE id = %s",
        (int(send_ref[1:]),),
    )
    names = [d.name for d in cur.description]
    return dict(zip(names, await cur.fetchone(), strict=True))


async def count_sends(conn, session_id=None) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM sends WHERE %(s)s::uuid IS NULL OR session_id = %(s)s::uuid",
        {"s": session_id},
    )
    return (await cur.fetchone())[0]


async def bindings_snapshot(conn) -> list[tuple]:
    cur = await conn.execute(
        "SELECT id, room_id, session_id, origin, released_at FROM room_bindings ORDER BY id"
    )
    return await cur.fetchall()


# --- client ------------------------------------------------------------------------------------


@pytest.fixture
async def agent_for(pool):
    clients = []

    def make(**extra) -> httpx.AsyncClient:
        app = create_agent_app(pool, None, make_settings(agent_token_sha256=DIGEST, **extra))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agents" + CHAT_PREFIX
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


@pytest.fixture
def agent(agent_for):
    return agent_for()


def as_(session_id, slug="hive-1") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Orch-Session": str(session_id)}
    if slug is not None:
        headers["X-Orch-Slug"] = slug
    return headers


async def say(agent, session_id, text, **body) -> httpx.Response:
    return await agent.post("/messages", json={"text": text, **body}, headers=as_(session_id))


async def react(agent, session_id, to, key="\U0001f44d") -> httpx.Response:
    return await agent.post("/reactions", json={"to": to, "key": key}, headers=as_(session_id))


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


def refused(response: httpx.Response, status: int, error: str) -> None:
    assert response.status_code == status, response.text
    assert code(response) == error, response.text


def accepted(response: httpx.Response) -> str:
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert set(body) == {"id", "status"} and body["id"].startswith("s")
    return body["id"]


async def listed(agent, session_id) -> list[dict]:
    r = await agent.get("/rooms", headers=as_(session_id))
    assert r.status_code == 200, r.text
    return r.json()["rooms"]


# --- pure helpers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "parsed"),
    [
        ("m1", ("m", 1)),
        ("s42", ("s", 42)),
        (f"m{2**63 - 1}", ("m", 2**63 - 1)),
        (f"m{2**63}", None),
        ("m0", None),
        ("m01", None),
        ("M1", None),
        ("m1 ", None),
        (" s1", None),
        ("s-1", None),
        ("x1", None),
        ("m", None),
        ("m١", None),  # an Arabic-Indic digit is not an ASCII digit
        ("$event:x", None),
        ("!room:x", None),
        ("", None),
    ],
)
def test_parse_reference(ref, parsed):
    assert parse_ref(ref) == parsed


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        ("\U0001f44d", True),
        ("❤️", True),  # heart with a variation selector
        ("\U0001f468‍\U0001f469‍\U0001f467", True),  # a ZWJ family
        ("\U0001f44d\U0001f3fd", True),  # a skin tone modifier
        ("\U0001f44d" * 16, True),
        ("\U0001f44d" * 17, False),
        ("", False),
        ("+1", False),
        ("a", False),
        ("\U0001f44d1", False),
        ("\U0001f44d ", False),
        ("\U0001f44d ", False),  # no-break space
        ("\U0001f44d ", False),  # em space
        ("\U0001f44d\u0085", False),  # next line, whitespace and control
        ("\U0001f44d\u0080", False),  # a C1 control character
        ("\U0001f44d\x00", False),
        ("\U0001f44d\n", False),
    ],
)
def test_valid_key(key, ok):
    assert valid_key(key) is ok


# --- routes ------------------------------------------------------------------------------------


async def test_send_to_flagged_room_without_inbound(agent, conn):
    room = "!dm:x"
    await add_room(conn, room, flag=True)
    await allow_room(conn, room)
    session = uuid.uuid4()

    send_id = accepted(await say(agent, session, "hello there", room=room))

    assert await send_row(conn, send_id) == {
        "session_id": session,
        "slug": "hive-1",
        "kind": "message",
        "room_id": room,
        "text": "hello there",
        "key": None,
        "reply_to": None,
        "ref_event_id": None,
        "status": "queued",
    }
    assert await bindings_snapshot(conn) == []
    for table in ("inbound", "runs", "outbound"):
        cur = await conn.execute(f"SELECT count(*) FROM {table}")
        assert (await cur.fetchone())[0] == 0, table


async def test_room_choice(agent, conn):
    home, flagged = "!home:x", "!flag:x"
    await add_room(conn, home, flag=False)
    await add_room(conn, flagged, flag=True)
    for room in (home, flagged):
        await allow_room(conn, room)
    bound, unbound = uuid.uuid4(), uuid.uuid4()
    await bind(conn, home, bound)

    given = accepted(await say(agent, bound, "given", room=home))
    assert (await send_row(conn, given))["room_id"] == home
    omitted = accepted(await say(agent, bound, "omitted"))
    assert (await send_row(conn, omitted))["room_id"] == home
    nulled = accepted(await say(agent, bound, "nulled", room=None, reply_to=None))
    assert (await send_row(conn, nulled))["room_id"] == home

    before = await count_sends(conn)
    refused(await say(agent, unbound, "nowhere"), 422, "room_required")
    assert await count_sends(conn) == before

    ref, event_id = await add_inbound(conn, flagged)
    reply = accepted(await say(agent, unbound, "derived", reply_to=ref))
    row = await send_row(conn, reply)
    assert row["room_id"] == flagged and row["reply_to"] == ref
    assert row["ref_event_id"] == event_id

    # The referenced message's room wins over the session's bound room, and is then checked; a
    # room that is not the session's own is answered as an unusable reference.
    refused(await say(agent, bound, "elsewhere", reply_to=ref), 422, "bad_reference")
    home_ref, _ = await add_inbound(conn, home)
    in_home = accepted(await say(agent, bound, "home reply", reply_to=home_ref))
    assert (await send_row(conn, in_home))["room_id"] == home


async def test_room_refusals_queue_nothing(agent, conn):
    denied, default, unflagged, flag_off = "!denied:x", "!default:x", "!unflagged:x", "!off:x"
    await add_room(conn, denied, flag=True)
    await add_rule(conn, "deny", "room", denied)
    await add_room(conn, default, members=(CAROL,), flag=True)
    await add_room(conn, unflagged)
    await allow_room(conn, unflagged)
    await add_room(conn, flag_off, flag=False)
    await allow_room(conn, flag_off)
    session = uuid.uuid4()

    unknown = await say(agent, session, "x", room="!nope:x")
    refused(unknown, 403, "room_not_allowed")
    for room in (denied, default):
        r = await say(agent, session, "x", room=room)
        refused(r, 403, "room_not_allowed")
        assert r.json() == unknown.json()  # no probing of room existence
    for room in (unflagged, flag_off):
        refused(await say(agent, session, "x", room=room), 403, "room_not_authorised")

    assert await count_sends(conn) == 0
    assert await bindings_snapshot(conn) == []


async def test_binding_refusals(agent, conn):
    other_bound, reserved, own, second, own_denied = (
        "!other:x",
        "!reserved:x",
        "!own:x",
        "!second:x",
        "!owndenied:x",
    )
    for room in (other_bound, reserved, own, second, own_denied):
        await add_room(conn, room, flag=True)
        await allow_room(conn, room)
    await add_rule(conn, "deny", "room", own_denied)
    other, unbound, bound, bound_denied = (uuid.uuid4() for _ in range(4))
    await bind(conn, other_bound, other)
    await reserve(conn, reserved)
    await bind(conn, own, bound)
    await bind(conn, own_denied, bound_denied)
    before = await bindings_snapshot(conn)

    for session in (unbound, bound):
        for room in (other_bound, reserved):
            refused(await say(agent, session, "x", room=room), 409, "room_bound")
    refused(await say(agent, bound, "x", room=second), 409, "session_bound")
    refused(await say(agent, bound_denied, "x", room=own_denied), 403, "room_not_allowed")

    assert await count_sends(conn) == 0
    assert await bindings_snapshot(conn) == before


async def error_keys(response) -> set[str]:
    return set(response.json()["error"])


async def test_room_bound_names_occupier_when_room_flagged(agent, conn):
    await add_room(conn, "!occ:x", flag=True)
    await allow_room(conn, "!occ:x")
    occupier, caller = uuid.uuid4(), uuid.uuid4()
    await bind(conn, "!occ:x", occupier)
    r = await say(agent, caller, "x", room="!occ:x")
    refused(r, 409, "room_bound")
    assert r.json()["error"]["occupier"] == str(occupier)
    assert await count_sends(conn) == 0


async def test_room_bound_hides_occupier_when_not_flagged(agent, conn):
    await add_room(conn, "!occ:x", flag=False)
    await allow_room(conn, "!occ:x")
    await bind(conn, "!occ:x", uuid.uuid4())
    r = await say(agent, uuid.uuid4(), "x", room="!occ:x")
    refused(r, 409, "room_bound")
    assert await error_keys(r) == {"code", "message"}


async def test_room_bound_reservation_has_no_occupier(agent, conn):
    await add_room(conn, "!occ:x", flag=True)
    await allow_room(conn, "!occ:x")
    await reserve(conn, "!occ:x")
    r = await say(agent, uuid.uuid4(), "x", room="!occ:x")
    refused(r, 409, "room_bound")
    assert await error_keys(r) == {"code", "message"}


async def test_reply_masking_still_wins_over_occupier(agent, conn):
    await add_room(conn, "!occ:x", flag=True)
    await allow_room(conn, "!occ:x")
    await bind(conn, "!occ:x", uuid.uuid4())
    ref, _ = await add_inbound(conn, "!occ:x")
    r = await say(agent, uuid.uuid4(), "x", reply_to=ref)
    refused(r, 422, "bad_reference")
    assert "occupier" not in r.json()["error"]


async def test_other_refusals_carry_no_occupier(agent, conn):
    denied, unflagged, flagged = "!denied:x", "!unflagged:x", "!flagged:x"
    await add_room(conn, denied, flag=True)
    await add_room(conn, unflagged, flag=False)
    await allow_room(conn, unflagged)
    await add_room(conn, flagged, flag=True)
    await allow_room(conn, flagged)
    bound_room, bound = "!bound:x", uuid.uuid4()
    await add_room(conn, bound_room, flag=True)
    await allow_room(conn, bound_room)
    await bind(conn, bound_room, bound)
    cases = [
        (uuid.uuid4(), denied, 403, "room_not_allowed"),
        (uuid.uuid4(), unflagged, 403, "room_not_authorised"),
        (bound, flagged, 409, "session_bound"),
    ]
    for session, room, status, error in cases:
        r = await say(agent, session, "x", room=room)
        refused(r, status, error)
        assert await error_keys(r) == {"code", "message"}


async def test_policy_member_rules(agent_for, conn, monkeypatch):
    calls = []
    real = agent_api.agent_room_decision

    def spy(room, rule_list, settings):
        calls.append(room.room_id)
        return real(room, rule_list, settings)

    monkeypatch.setattr(agent_api, "agent_room_decision", spy)
    by_facebook, group, clean_group = "!fb:x", "!grp:x", "!clean:x"
    await add_room(conn, by_facebook, members=(ALICE, BOT), flag=True)
    await add_rule(conn, "allow", "facebook", "111")
    await add_room(conn, group, kind="group", members=(CAROL, BOB, BOT), flag=True)
    await add_rule(conn, "deny", "user", BOB)
    await add_room(conn, clean_group, kind="group", members=(CAROL, BOT), flag=True)
    session = uuid.uuid4()

    deny_default = agent_for()
    accepted(await say(deny_default, session, "fb", room=by_facebook))
    refused(await say(deny_default, session, "clean", room=clean_group), 403, "room_not_allowed")
    assert by_facebook in calls and clean_group in calls

    allow_default = agent_for(default_policy="allow")
    refused(await say(allow_default, session, "grp", room=group), 403, "room_not_allowed")
    accepted(await say(allow_default, session, "clean", room=clean_group))
    assert [r["room_id"] for r in await listed(allow_default, session)] == sorted(
        [by_facebook, clean_group]
    )
    assert group in calls


async def test_derived_room_authorised(agent, conn):
    denied, other_bound, unflagged, flagged, home = (
        "!denied:x",
        "!other:x",
        "!unflagged:x",
        "!flagged:x",
        "!home:x",
    )
    await add_room(conn, denied, flag=True)
    await add_rule(conn, "deny", "room", denied)
    for room, flag in ((other_bound, True), (unflagged, False), (flagged, True), (home, False)):
        await add_room(conn, room, flag=flag)
        await allow_room(conn, room)
    unbound, bound, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await bind(conn, other_bound, other)
    await bind(conn, home, bound)
    m_denied, _ = await add_inbound(conn, denied, status="ignored", reason="denied: room")
    m_other, _ = await add_inbound(conn, other_bound)
    m_unflagged, _ = await add_inbound(conn, unflagged)
    m_flagged, _ = await add_inbound(conn, flagged)
    s_home = await add_send(conn, bound, home)
    before_sends, before_bindings = await count_sends(conn), await bindings_snapshot(conn)

    # A reference into a room that is not the session's own answers like an unknown id.
    for session, ref in (
        (unbound, m_denied),
        (unbound, m_other),
        (bound, m_other),
        (unbound, m_unflagged),
        (bound, m_flagged),
    ):
        refused(await say(agent, session, "reply", reply_to=ref), 422, "bad_reference")
        refused(await react(agent, session, ref), 422, "bad_reference")

    # The session's own room keeps its informative refusal: a room that turns denied refuses
    # replies and reactions to the session's own sent send and to an inbound message of it.
    m_home, _ = await add_inbound(conn, home)
    await add_rule(conn, "deny", "room", home)
    refused(await say(agent, bound, "reply", reply_to=s_home), 403, "room_not_allowed")
    refused(await react(agent, bound, s_home), 403, "room_not_allowed")
    refused(await say(agent, bound, "reply", reply_to=m_home), 403, "room_not_allowed")
    refused(await react(agent, bound, m_home), 403, "room_not_allowed")

    assert await count_sends(conn) == before_sends
    assert await bindings_snapshot(conn) == before_bindings


async def test_reply_references(agent, conn):
    home, other_room = "!home:x", "!otherroom:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    await add_room(conn, other_room, flag=True)
    await allow_room(conn, other_room)
    session, other = uuid.uuid4(), uuid.uuid4()
    await bind(conn, home, session)
    m_home, m_home_event = await add_inbound(conn, home)
    m_elsewhere, _ = await add_inbound(conn, other_room)
    s_sent = await add_send(conn, session, home, event_id="$out-home")
    s_queued = await add_send(conn, session, home, status="queued")
    s_sending = await add_send(conn, session, home, status="sending")
    s_failed = await add_send(conn, session, home, status="failed")
    s_other = await add_send(conn, other, home)
    s_reaction = await add_send(conn, session, home, kind="reaction", key="\U0001f44d")

    to_inbound = accepted(await say(agent, session, "to m", reply_to=m_home))
    row = await send_row(conn, to_inbound)
    assert row["reply_to"] == m_home and row["ref_event_id"] == m_home_event
    to_send = accepted(await say(agent, session, "to s", room=home, reply_to=s_sent))
    row = await send_row(conn, to_send)
    assert row["reply_to"] == s_sent and row["ref_event_id"] == "$out-home"

    before = await count_sends(conn)
    bad = [
        "m999999",
        "s999999",
        s_queued,
        s_sending,
        s_failed,
        s_other,
        s_reaction,
        m_home_event,
        home,
        "m0",
        f"m{2**63}",
        "",
    ]
    for ref in bad:
        refused(await say(agent, session, "bad", reply_to=ref), 422, "bad_reference")
    refused(await say(agent, session, "bad", room=home, reply_to=m_elsewhere), 422, "bad_reference")
    # Reference resolution comes before room choice and the room checks.
    refused(
        await say(agent, session, "bad", room="!nope:x", reply_to="m999999"), 422, "bad_reference"
    )
    assert await count_sends(conn) == before


async def test_reactions(agent, conn):
    home, flagged = "!home:x", "!flag:x"
    for room, flag in ((home, False), (flagged, True)):
        await add_room(conn, room, flag=flag)
        await allow_room(conn, room)
    session, unbound = uuid.uuid4(), uuid.uuid4()
    await bind(conn, home, session)
    m_home, m_home_event = await add_inbound(conn, home)
    m_flagged, m_flagged_event = await add_inbound(conn, flagged)
    s_sent = await add_send(conn, session, home, event_id="$out-home")
    s_reaction = await add_send(conn, session, home, kind="reaction", key="\U0001f44d")

    reaction = accepted(await react(agent, session, m_home, "\U0001f44d"))
    assert await send_row(conn, reaction) == {
        "session_id": session,
        "slug": "hive-1",
        "kind": "reaction",
        "room_id": home,
        "text": None,
        "key": "\U0001f44d",
        "reply_to": m_home,
        "ref_event_id": m_home_event,
        "status": "queued",
    }
    to_send = accepted(await react(agent, session, s_sent, "❤️"))
    assert (await send_row(conn, to_send))["ref_event_id"] == "$out-home"
    long_key = accepted(await react(agent, session, m_home, "\U0001f44d" * 16))
    assert (await send_row(conn, long_key))["key"] == "\U0001f44d" * 16

    initiation = accepted(await react(agent, unbound, m_flagged))
    row = await send_row(conn, initiation)
    assert row["room_id"] == flagged and row["ref_event_id"] == m_flagged_event

    before = await count_sends(conn)
    for key in ("+1", "a", "\U0001f44d ", "\U0001f44d ", "\U0001f44d\u0080", ""):
        refused(await react(agent, session, m_home, key), 422, "invalid_key")
    refused(await react(agent, session, m_home, "\U0001f44d" * 17), 422, "invalid_key")
    # Key validation comes before reference resolution.
    refused(await react(agent, session, "bogus", "+1"), 422, "invalid_key")
    for target in (s_reaction, m_home_event, "m999999"):
        refused(await react(agent, session, target), 422, "bad_reference")
    for body in (
        {"to": m_home, "key": 1},
        {"to": m_home},
        {"key": "\U0001f44d"},
        {"to": m_home, "key": "\U0001f44d", "room": home},
    ):
        r = await agent.post("/reactions", json=body, headers=as_(session))
        refused(r, 422, "invalid_request")
    assert await count_sends(conn) == before


async def test_duplicate_returns_existing_send(agent, conn):
    home = "!home:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    session = uuid.uuid4()
    await bind(conn, home, session)
    m_home, _ = await add_inbound(conn, home)

    first = accepted(await say(agent, session, "same", room=home))
    for status in ("queued", "sending", "sent"):
        await set_send(conn, first, status=status)
        for body in ({"room": home}, {}):  # an omitted room resolves to the same room
            r = await say(agent, session, "same", **body)
            assert r.status_code == 202, r.text
            assert r.json() == {"id": first, "status": status}
    assert await count_sends(conn, session) == 1

    changed_text = accepted(await say(agent, session, "same!", room=home))
    as_reply = accepted(await say(agent, session, "same", reply_to=m_home))
    r = await say(agent, session, "same", room=home, reply_to=m_home)
    assert r.json() == {"id": as_reply, "status": "queued"}
    assert len({first, changed_text, as_reply}) == 3

    reaction = accepted(await react(agent, session, m_home, "\U0001f44d"))
    assert (await react(agent, session, m_home, "\U0001f44d")).json() == {
        "id": reaction,
        "status": "queued",
    }
    other_key = accepted(await react(agent, session, m_home, "\U0001f44e"))
    assert other_key != reaction
    assert await count_sends(conn, session) == 5

    for status in ("failed", "refused"):
        await set_send(conn, first, status=status, reason="forbidden")
        retried = accepted(await say(agent, session, "same", room=home))
        assert retried != first
        await set_send(conn, retried, status=status, reason="forbidden")
    await age_sends(conn, session)
    aged = await count_sends(conn, session)
    fresh = accepted(await react(agent, session, m_home, "\U0001f44e"))
    assert fresh != other_key
    assert await count_sends(conn, session) == aged + 1

    # At the rate limit a duplicate still gets its send; a new request does not.
    for n in range(19):
        await add_send(conn, session, home, text=f"filler {n}", status="queued")
    full = await count_sends(conn, session)
    r = await react(agent, session, m_home, "\U0001f44e")
    assert r.status_code == 202 and r.json() == {"id": fresh, "status": "queued"}
    refused(await say(agent, session, "brand new", room=home), 429, "rate_limited")
    assert await count_sends(conn, session) == full


async def test_rate_limit(agent_for, conn):
    agent = agent_for(agent_rate_per_minute=3)
    home, other_home = "!home:x", "!otherhome:x"
    for room in (home, other_home):
        await add_room(conn, room)
        await allow_room(conn, room)
    session, other = uuid.uuid4(), uuid.uuid4()
    await bind(conn, home, session)
    await bind(conn, other_home, other)
    m_home, _ = await add_inbound(conn, home)

    first = accepted(await say(agent, session, "one"))
    accepted(await say(agent, session, "two"))
    accepted(await react(agent, session, m_home))
    await set_send(conn, first, status="failed", reason="forbidden")  # any status counts
    refused(await say(agent, session, "three"), 429, "rate_limited")
    refused(await react(agent, session, m_home, "\U0001f44e"), 429, "rate_limited")
    assert await count_sends(conn, session) == 3

    accepted(await say(agent, other, "unaffected"))
    assert await count_sends(conn, other) == 1

    await age_sends(conn, session)
    accepted(await say(agent, session, "three"))
    assert await count_sends(conn, session) == 4


async def test_parallel_sends_respect_rate_limit(agent_for, conn):
    agent = agent_for(agent_rate_per_minute=3)
    home = "!home:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    session = uuid.uuid4()
    await bind(conn, home, session)

    results = await asyncio.gather(*(say(agent, session, f"parallel {n}") for n in range(6)))

    assert sorted(r.status_code for r in results) == [202, 202, 202, 429, 429, 429]
    for r in results:
        if r.status_code == 429:
            assert code(r) == "rate_limited"
    assert await count_sends(conn, session) == 3


def rendezvous(monkeypatch, name: str, parties: int) -> None:
    """Make the first `parties` calls of `directory.<name>` wait for each other, so every
    request is at that call at the same moment: a request that still holds a pool connection
    there is then holding one while all the others do too."""
    real = getattr(agent_api.directory, name)
    barrier = asyncio.Barrier(parties)
    arrived = 0

    async def wrapper(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived <= parties:
            await asyncio.wait_for(barrier.wait(), timeout=5)
        return await real(*args, **kwargs)

    monkeypatch.setattr(agent_api.directory, name, wrapper)


async def test_concurrent_requests_do_not_exhaust_pool(url, conn, monkeypatch):
    size = 4
    small = AsyncConnectionPool(url, min_size=size, max_size=size, timeout=3, open=False)
    await small.open(wait=True)
    try:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_agent_app(small, None, make_settings(agent_token_sha256=DIGEST))
            ),
            base_url="http://agents" + CHAT_PREFIX,
        )
        sessions = [uuid.uuid4() for _ in range(size)]
        for n, session in enumerate(sessions):
            home = f"!home{n}:x"
            await add_room(conn, home, flag=False)
            await allow_room(conn, home)
            await bind(conn, home, session)
        await add_room(conn, "!open:x", flag=True)
        await allow_room(conn, "!open:x")

        # N requests, pool of N: every request is at the room read holding no second connection.
        rendezvous(monkeypatch, "get_room", size)
        sent = await asyncio.gather(*(say(client, s, "hi") for s in sessions))
        assert [r.status_code for r in sent] == [202] * size, [r.text for r in sent]

        rendezvous(monkeypatch, "list_rooms", size)
        listings = await asyncio.gather(*(client.get("/rooms", headers=as_(s)) for s in sessions))
        assert [r.status_code for r in listings] == [200] * size, [r.text for r in listings]
        for n, response in enumerate(listings):
            assert [room["room_id"] for room in response.json()["rooms"]] == [f"!home{n}:x"]
        await client.aclose()
    finally:
        await small.close()


async def test_room_hourly_chunk_cap(agent_for, conn):
    agent = agent_for(agent_room_chunks_per_hour=5, reply_chunk_chars=100)
    home, other_home = "!home:x", "!otherhome:x"
    for room in (home, other_home):
        await add_room(conn, room)
        await allow_room(conn, room)
    session, other = uuid.uuid4(), uuid.uuid4()
    await bind(conn, home, session)
    await bind(conn, other_home, other)
    m_home, _ = await add_inbound(conn, home)

    accepted(await say(agent, session, "a" * 250))  # 3 chunks
    before = await count_sends(conn, session)
    r = await say(agent, session, "b" * 250)  # 3 more would make 6
    refused(r, 429, "room_chunk_limit")
    assert "5 chunks" in r.json()["error"]["message"]
    refused(await say(agent, session, "b" * 250, room=home), 429, "room_chunk_limit")
    assert await count_sends(conn, session) == before

    accepted(await say(agent, session, "c" * 150))  # 2 chunks: exactly the cap
    refused(await say(agent, session, "d"), 429, "room_chunk_limit")
    refused(await react(agent, session, m_home), 429, "room_chunk_limit")  # a reaction is 1
    accepted(await say(agent, other, "a" * 250))  # another room has its own budget

    # A duplicate of an accepted send is still answered at the cap.
    assert (await say(agent, session, "c" * 150)).status_code == 202

    await age_sends(conn, session, 3601)
    accepted(await say(agent, session, "b" * 250))


async def test_room_chunk_cap_counts_failed_not_refused(agent_for, conn):
    agent = agent_for(agent_room_chunks_per_hour=2)
    home = "!home:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    session = uuid.uuid4()
    await bind(conn, home, session)

    first = accepted(await say(agent, session, "one"))
    second = accepted(await say(agent, session, "two"))
    await set_send(conn, first, status="failed", reason="forbidden")
    refused(await say(agent, session, "three"), 429, "room_chunk_limit")
    await set_send(conn, second, status="refused", reason="room_bound")
    accepted(await say(agent, session, "three"))


async def test_parallel_sessions_respect_room_chunk_cap(agent_for, conn):
    agent = agent_for(agent_room_chunks_per_hour=3)
    room = "!flag:x"
    await add_room(conn, room, flag=True)
    await allow_room(conn, room)
    sessions = [uuid.uuid4() for _ in range(6)]

    results = await asyncio.gather(*(say(agent, s, "hi", room=room) for s in sessions))

    assert sorted(r.status_code for r in results) == [202, 202, 202, 429, 429, 429]
    for r in results:
        if r.status_code == 429:
            assert code(r) == "room_chunk_limit"
    assert await count_sends(conn) == 3


@pytest.mark.parametrize(
    "slug",
    [
        "Upper",
        "has space",
        "-lead",
        "trail-",
        "under_score",
        "a.b",
        "a/b",
        "orchestrator",
        "orchestrator-x",
        "a" * 41,
        "a" * 5000,
    ],
)
async def test_invalid_slug_header_rejected(agent, conn, slug):
    home = "!home:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    session = uuid.uuid4()
    await bind(conn, home, session)
    m_home, _ = await add_inbound(conn, home)

    headers = as_(session, slug=slug)
    r = await agent.post("/messages", json={"text": "hi"}, headers=headers)
    refused(r, 400, "invalid_slug")
    r = await agent.post("/reactions", json={"to": m_home, "key": "\U0001f44d"}, headers=headers)
    refused(r, 400, "invalid_slug")
    refused(await agent.get("/rooms", headers=headers), 400, "invalid_slug")
    assert await count_sends(conn) == 0

    # The session identity is checked first.
    r = await agent.post(
        "/messages", json={"text": "hi"}, headers={**headers, "X-Orch-Session": "nope"}
    )
    refused(r, 401, "unauthorized")


async def test_valid_slugs_stored(agent, conn):
    home = "!home:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    session = uuid.uuid4()
    await bind(conn, home, session)

    for n, slug in enumerate(("a", "mx-12", "orchestrators", "a" * 40, "0-9")):
        r = await agent.post("/messages", json={"text": f"hi {n}"}, headers=as_(session, slug=slug))
        assert (await send_row(conn, accepted(r)))["slug"] == slug


async def test_foreign_reference_errors_are_uniform(agent, conn):
    bound_elsewhere, denied, unflagged, own = "!other:x", "!denied:x", "!unflagged:x", "!own:x"
    await add_room(conn, bound_elsewhere, flag=True)
    await add_room(conn, denied, flag=True)
    await add_rule(conn, "deny", "room", denied)
    await add_room(conn, unflagged, flag=False)
    await add_room(conn, own, flag=False)
    for room in (bound_elsewhere, unflagged, own):
        await allow_room(conn, room)
    stranger, bound, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await bind(conn, bound_elsewhere, other)
    await bind(conn, own, bound)
    foreign = [
        (await add_inbound(conn, room))[0] for room in (bound_elsewhere, denied, unflagged)
    ] + ["m999999"]
    before = await count_sends(conn)

    for session in (stranger, bound):
        answers = []
        for ref in foreign:
            message = await say(agent, session, "probe", reply_to=ref)
            reaction = await react(agent, session, ref)
            refused(message, 422, "bad_reference")
            refused(reaction, 422, "bad_reference")
            answers.append(message.json()["error"]["message"].split(": ", 1)[1])
            answers.append(reaction.json()["error"]["message"].split(": ", 1)[1])
        assert len(set(answers)) == 1
    assert await count_sends(conn) == before


async def test_validation(agent, conn):
    home = "!home:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    session = uuid.uuid4()
    await bind(conn, home, session)

    for body in (
        {"text": ""},
        {"text": "a" * 16001},
        {"text": "\U0001f600" * 16001},
        {},
        {"room": home},
        {"text": 5},
        {"text": "x", "extra": 1},
        {"text": "x", "room": 5},
        {"text": "x", "reply_to": 5},
        {"text": "x", "key": "\U0001f44d"},
    ):
        r = await agent.post("/messages", json=body, headers=as_(session))
        refused(r, 422, "invalid_request")
    r = await agent.post(
        "/messages",
        content=b"{not json",
        headers={**as_(session), "content-type": "application/json"},
    )
    refused(r, 422, "invalid_request")
    assert await count_sends(conn) == 0

    raw_text = "\U0001f600" * 16000
    raw = json.dumps({"text": raw_text}, ensure_ascii=False).encode()
    assert len(raw_text.encode()) == 64000
    r = await agent.post(
        "/messages", content=raw, headers={**as_(session), "content-type": "application/json"}
    )
    raw_id = accepted(r)
    assert (await send_row(conn, raw_id))["text"] == raw_text
    escaped_text = "\U0001f601" * 16000
    escaped = json.dumps({"text": escaped_text}, ensure_ascii=True).encode()
    r = await agent.post(
        "/messages", content=escaped, headers={**as_(session), "content-type": "application/json"}
    )
    escaped_id = accepted(r)
    assert (await send_row(conn, escaped_id))["text"] == escaped_text
    plain_max = accepted(await say(agent, session, "a" * 16000))
    assert len((await send_row(conn, plain_max))["text"]) == 16000

    no_slug = await agent.post(
        "/messages", json={"text": "no slug"}, headers=as_(session, slug=None)
    )
    assert (await send_row(conn, accepted(no_slug)))["slug"] is None

    before = await count_sends(conn)
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    for headers in (
        bearer,
        {**bearer, "X-Orch-Session": ""},
        {**bearer, "X-Orch-Session": "not-a-uuid"},
        {**bearer, "X-Orch-Session": str(session)[:-1]},
        {**bearer, "X-Orch-Session": f"{session} extra"},
    ):
        for method, path, body in (
            ("GET", "/rooms", None),
            ("POST", "/messages", {"text": "x"}),
            ("POST", "/messages", {"bogus": True}),  # the session check comes before the body
            ("POST", "/reactions", {"to": "m1", "key": "\U0001f44d"}),
            ("GET", f"/messages/{raw_id}", None),
        ):
            r = await agent.request(method, path, json=body, headers=headers)
            refused(r, 401, "unauthorized")
    assert await count_sends(conn) == before


async def test_whitespace_only_text_refused(agent, conn):
    home = "!home:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    session = uuid.uuid4()
    await bind(conn, home, session)

    for text in (" ", "\n", " \t\r\n ", "\u00a0", "\u2003\u3000", "\n \n\t\n"):
        r = await say(agent, session, text)
        refused(r, 422, "invalid_text")
        r = await say(agent, session, text, room=home)
        refused(r, 422, "invalid_text")
    assert await count_sends(conn) == 0

    kept = accepted(await say(agent, session, "  hi  "))
    assert (await send_row(conn, kept))["text"] == "  hi  "


async def test_rooms_listing_order(agent, conn):
    rooms = {
        "!b1:x": "Beta",
        "!a2:x": "Alpha",
        "!a1:x": "Alpha",
        "!c1:x": "alpha",
        "!z0:x": None,
        "!y0:x": None,
    }
    for room_id, name in rooms.items():
        await add_room(conn, room_id, name=name, kind="group", flag=True)
        await allow_room(conn, room_id)
    excluded = {
        "!other:x": dict(flag=True),
        "!reserved:x": dict(flag=True),
        "!unflagged:x": dict(flag=False),
        "!noflag:x": dict(),
        "!denied:x": dict(flag=True),
        "!enc:x": dict(flag=True, enc=True),
    }
    for room_id, options in excluded.items():
        await add_room(conn, room_id, name="Aardvark", **options)
        await allow_room(conn, room_id)
    await add_rule(conn, "deny", "room", "!denied:x")
    await add_room(conn, "!own:x", name="Zed", kind="dm", flag=False)
    await allow_room(conn, "!own:x")
    await add_room(conn, "!owndenied:x", name="Own denied", flag=True)
    await add_rule(conn, "deny", "room", "!owndenied:x")
    other, unbound, bound, bound_denied = (uuid.uuid4() for _ in range(4))
    await bind(conn, "!other:x", other)
    await reserve(conn, "!reserved:x")
    await bind(conn, "!own:x", bound)
    await bind(conn, "!owndenied:x", bound_denied)

    unbound_rooms = await listed(agent, unbound)
    assert [r["room_id"] for r in unbound_rooms] == [
        "!a1:x",
        "!a2:x",
        "!b1:x",
        "!c1:x",
        "!y0:x",
        "!z0:x",
    ]
    assert unbound_rooms[0] == {
        "room_id": "!a1:x",
        "name": "Alpha",
        "kind": "group",
        "bound": False,
        "agents_may_initiate": True,
    }
    assert all(r["bound"] is False and r["agents_may_initiate"] for r in unbound_rooms)

    assert await listed(agent, bound) == [
        {
            "room_id": "!own:x",
            "name": "Zed",
            "kind": "dm",
            "bound": True,
            "agents_may_initiate": False,
        }
    ]
    assert await listed(agent, bound_denied) == []
    assert [r["room_id"] for r in await listed(agent, other)] == ["!other:x"]


async def test_listing_matches_send_checks(agent, conn):
    policies = ("room_rule", "member_rule", "deny_room", "deny_member")
    states = ("caller", "other", "reserved", "flagged", "neither")
    await add_rule(conn, "allow", "facebook", "111")
    await add_rule(conn, "deny", "user", BOB)
    callers = {policy: uuid.uuid4() for policy in policies}
    unbound = uuid.uuid4()
    room_of: dict[tuple[str, str], str] = {}
    targets: dict[str, str] = {}
    for policy in policies:
        for state in states:
            room_id = f"!{policy}-{state}:x"
            room_of[(policy, state)] = room_id
            members = {
                "room_rule": (CAROL,),
                "member_rule": (ALICE, BOT),
                "deny_room": (ALICE,),
                "deny_member": (ALICE, BOB),
            }[policy]
            await add_room(conn, room_id, members=members, flag=state != "neither")
            if policy == "room_rule":
                await allow_room(conn, room_id)
            if policy == "deny_room":
                await add_rule(conn, "deny", "room", room_id)
            if state == "caller":
                await bind(conn, room_id, callers[policy])
            elif state == "other":
                await bind(conn, room_id, uuid.uuid4())  # one room per session
            elif state == "reserved":
                await reserve(conn, room_id)
            targets[room_id], _ = await add_inbound(conn, room_id)

    own_room = {callers[policy]: room_of[(policy, "caller")] for policy in policies}
    allowed = {"room_rule", "member_rule"}
    expected = {
        unbound: {room_of[(p, "flagged")] for p in allowed},
        **{
            session: ({room_of[(policy, "caller")]} if policy in allowed else set())
            for policy, session in callers.items()
        },
    }
    for session, rooms in expected.items():
        listing = {r["room_id"] for r in await listed(agent, session)}
        assert listing == rooms
        for room_id, target in targets.items():
            message = await say(agent, session, f"{session} {room_id}", room=room_id)
            reaction = await react(agent, session, target)
            assert (message.status_code == 202) == (room_id in listing), (room_id, message.text)
            if message.status_code == 202:
                assert reaction.status_code == 202, (room_id, reaction.text)
            elif room_id == own_room.get(session):
                assert code(reaction) == code(message)  # the session's own room is explained
            else:
                refused(reaction, 422, "bad_reference")  # another room's message is not visible


async def test_status_own_sends_only(agent, conn):
    home = "!home:x"
    await add_room(conn, home)
    await allow_room(conn, home)
    session, other = uuid.uuid4(), uuid.uuid4()
    await bind(conn, home, session)
    m_home, _ = await add_inbound(conn, home)

    send_id = accepted(await say(agent, session, "hello"))
    r = await agent.get(f"/messages/{send_id}", headers=as_(session))
    assert r.status_code == 200, r.text
    body = r.json()
    created = datetime.fromisoformat(body.pop("created_at"))
    assert created.tzinfo is not None
    assert body == {
        "id": send_id,
        "kind": "message",
        "status": "queued",
        "reason": None,
        "room_id": home,
        "sent_at": None,
    }

    await set_send(conn, send_id, status="sent", event_id="$out", sent_at=created)
    body = (await agent.get(f"/messages/{send_id}", headers=as_(session))).json()
    assert body["status"] == "sent"
    assert datetime.fromisoformat(body["sent_at"]) == created

    reaction = accepted(await react(agent, session, m_home))
    await set_send(conn, reaction, status="refused", reason="room_bound")
    body = (await agent.get(f"/messages/{reaction}", headers=as_(session))).json()
    assert body["kind"] == "reaction" and body["status"] == "refused"
    assert body["reason"] == "room_bound"

    others = await add_send(conn, other, home)
    for ref in (others, "s999999", m_home, "m1", "abc", "1", f"s{2**63}"):
        r = await agent.get(f"/messages/{ref}", headers=as_(session))
        refused(r, 404, "not_found")
    assert (await agent.get(f"/messages/{others}", headers=as_(other))).status_code == 200


async def anonymize(conn, room_id):
    await conn.execute(
        "UPDATE room_settings SET anonymize_names = true WHERE room_id = %s", (room_id,)
    )


async def test_pseudonym_in_anonymized_room_refused(agent, conn):
    home, plain = "!home:x", "!plain:x"
    for room in (home, plain):
        await add_room(conn, room, flag=False)
        await allow_room(conn, room)
    await anonymize(conn, home)
    session, other = uuid.uuid4(), uuid.uuid4()
    await bind(conn, home, session)
    await bind(conn, plain, other)

    r = await say(agent, session, "Thanks User-0123456789Ab and admin-abcdefabcdef!")
    refused(r, 422, "pseudonym_disclosed")
    message = r.json()["error"]["message"]
    assert message.startswith("text: contains the pseudonym(s) User-0123456789Ab, admin-abcdefab")
    assert "must not be sent to the chat" in message
    assert "refer to the person differently" in message
    assert await count_sends(conn) == 0

    accepted(await say(agent, session, "Thanks Alice Smith, see user-0123"))
    accepted(await say(agent, other, "user-0123456789ab is fine where names are not hidden"))


async def test_pseudonym_check_after_room_checks(agent, conn):
    room = "!home:x"
    await add_room(conn, room, flag=False)
    await allow_room(conn, room)
    await anonymize(conn, room)
    await bind(conn, room, uuid.uuid4())
    r = await say(agent, uuid.uuid4(), "hi user-0123456789ab", room=room)
    refused(r, 409, "room_bound")


async def test_anonymized_room_names_listed_as_pseudonyms(agent, conn):
    dm, group = "!dm:x", "!group:x"
    await add_room(conn, dm, name="Alice A", flag=True)
    await add_room(
        conn, group, kind="group", name="Alice A, Bob B", members=(ALICE, BOB), flag=True
    )
    await conn.execute("UPDATE users SET display_name = 'Alice A' WHERE user_id = %s", (ALICE,))
    await conn.execute("UPDATE users SET display_name = 'Bob B' WHERE user_id = %s", (BOB,))
    for room in (dm, group):
        await allow_room(conn, room)
        await anonymize(conn, room)
    names = {r["room_id"]: r["name"] for r in await listed(agent, uuid.uuid4())}
    assert names == {dm: pseudonym(ALICE), group: f"{pseudonym(ALICE)}, {pseudonym(BOB)}"}


# --- room notes --------------------------------------------------------------------------------

NOTES_CANARY = "CANARY-NOTES-7f3a"


async def notes_get(agent, session_id) -> httpx.Response:
    return await agent.get("/notes", headers=as_(session_id))


async def notes_put(agent, session_id, body, version) -> httpx.Response:
    return await agent.put(
        "/notes", json={"body": body, "expected_version": version}, headers=as_(session_id)
    )


async def bound_session(conn, room=DM, slug="hive-1") -> uuid.UUID:
    session = uuid.uuid4()
    await bind(conn, room, session, slug)
    return session


async def test_notes_unbound_session_refused(agent, conn):
    session = uuid.uuid4()
    refused(await notes_get(agent, session), 409, "room_unbound")
    r = await notes_put(agent, session, "x", 0)
    refused(r, 409, "room_unbound")
    assert r.json()["error"]["message"] == "this session is not bound to a chat room"
    assert (await (await conn.execute("SELECT count(*) FROM room_notes")).fetchone())[0] == 0


async def test_notes_round_trip(agent, conn):
    session = await bound_session(conn)
    r = await notes_get(agent, session)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "room_id": DM,
        "body": "",
        "version": 0,
        "updated_at": None,
        "chars": 0,
        "max_chars": 8000,
        "rule": MEMORY_RULE,
    }
    r = await notes_put(agent, session, "## Requests\n- fix deck", 0)
    assert r.status_code == 200, r.text
    put = r.json()
    assert (put["room_id"], put["body"], put["version"], put["chars"]) == (
        DM,
        "## Requests\n- fix deck",
        1,
        len("## Requests\n- fix deck"),
    )
    assert put["updated_at"] is not None
    got = (await notes_get(agent, session)).json()
    assert got == put
    cleared = (await notes_put(agent, session, "", 1)).json()
    assert (cleared["body"], cleared["version"], cleared["chars"]) == ("", 2, 0)


async def test_notes_rule_override(agent_for, conn):
    session = await bound_session(conn)
    r = await notes_get(agent_for(room_memory_rule="R"), session)
    assert r.json()["rule"] == "R"


async def test_notes_isolated_between_rooms(agent, conn):
    a, b = await bound_session(conn, DM), await bound_session(conn, GRP, "hive-2")
    await notes_put(agent, a, "secret of A", 0)
    got = (await notes_get(agent, b)).json()
    assert (got["room_id"], got["body"], got["version"]) == (GRP, "", 0)
    r = await notes_put(agent, b, "B writes", 0)
    assert r.json()["room_id"] == GRP
    assert (await notes_get(agent, a)).json()["body"] == "secret of A"
    rows = await (await conn.execute("SELECT room_id, body FROM room_notes ORDER BY 1")).fetchall()
    assert rows == [(DM, "secret of A"), (GRP, "B writes")]


async def test_notes_follow_rebinding(agent, conn):
    a = await bound_session(conn, DM)
    await notes_put(agent, a, "room one notes", 0)
    await conn.execute(
        "UPDATE room_bindings SET released_at = now(), release_reason = 'test'"
        " WHERE session_id = %s",
        (a,),
    )
    c = await bound_session(conn, DM, "hive-3")
    got = (await notes_get(agent, c)).json()
    assert (got["body"], got["version"]) == ("room one notes", 1)
    refused(await notes_get(agent, a), 409, "room_unbound")
    refused(await notes_put(agent, a, "stale", 1), 409, "room_unbound")
    assert (await notes_get(agent, c)).json()["body"] == "room one notes"


async def test_notes_stale_version_conflict(agent, conn):
    session = await bound_session(conn)
    await notes_put(agent, session, "one", 0)
    assert (await notes_put(agent, session, "two", 1)).status_code == 200
    r = await notes_put(agent, session, "three", 1)
    refused(r, 409, "version_conflict")
    assert r.json()["error"]["message"] == (
        "changed since version 1; current version is 2: re-read and merge"
    )
    r = await notes_put(agent, session, "again", 0)
    refused(r, 409, "version_conflict")
    got = (await notes_get(agent, session)).json()
    assert (got["body"], got["version"]) == ("two", 2)


async def test_notes_cap_counts_code_points(agent, agent_for, conn):
    session = await bound_session(conn)
    emoji = "\U0001f600"
    r = await notes_put(agent, session, emoji * 8000, 0)
    assert r.status_code == 200, r.text
    assert r.json()["chars"] == 8000
    r = await notes_put(agent, session, emoji * 8001, 1)
    refused(r, 413, "notes_too_large")
    message = r.json()["error"]["message"]
    assert "8000" in message and "8001" in message
    small = agent_for(room_notes_max_chars=100)
    assert (await notes_put(small, session, "x" * 100, 1)).status_code == 200
    refused(await notes_put(small, session, "x" * 101, 2), 413, "notes_too_large")
    assert (await notes_get(small, session)).json()["max_chars"] == 100


async def test_notes_body_never_logged(agent, conn, caplog):
    caplog.set_level("DEBUG")
    session = await bound_session(conn)
    body = f"{NOTES_CANARY} " + "y" * 20
    r = await notes_put(agent, session, body, 0)
    assert r.status_code == 200
    await notes_get(agent, session)
    stale = await notes_put(agent, session, body, 0)
    big = await notes_put(agent, session, NOTES_CANARY * 2000, 1)
    bad = await agent.put(
        "/notes", json={"body": NOTES_CANARY, "expected_version": -1}, headers=as_(session)
    )
    assert bad.status_code == 422
    for response in (stale, big, bad):
        assert NOTES_CANARY not in response.text
    assert NOTES_CANARY not in caplog.text


async def test_notes_validation(agent, conn):
    session = await bound_session(conn)
    for payload in (
        {"body": "x"},
        {"expected_version": 0},
        {"body": "x", "expected_version": -1},
        {"body": "x", "expected_version": 0, "room": DM},
        {"body": 5, "expected_version": 0},
    ):
        r = await agent.put("/notes", json=payload, headers=as_(session))
        refused(r, 422, "invalid_request")
    assert (await notes_get(agent, session)).json()["version"] == 0


# --- room skills -------------------------------------------------------------------------------

SKILL_CANARY = "CANARY-SKILL-91bc"


async def skills_list(agent, session_id) -> httpx.Response:
    return await agent.get("/skills", headers=as_(session_id))


async def skill_get(agent, session_id, name) -> httpx.Response:
    return await agent.get(f"/skills/{name}", headers=as_(session_id))


async def skill_put(
    agent, session_id, name, version=0, *, description="when to use it", body="the steps"
) -> httpx.Response:
    return await agent.put(
        f"/skills/{name}",
        json={"description": description, "body": body, "expected_version": version},
        headers=as_(session_id),
    )


async def skill_delete(agent, session_id, name) -> httpx.Response:
    return await agent.delete(f"/skills/{name}", headers=as_(session_id))


async def test_skills_unbound_session_refused(agent, conn):
    session = uuid.uuid4()
    refused(await skills_list(agent, session), 409, "room_unbound")
    refused(await skill_get(agent, session, "deck-tune"), 409, "room_unbound")
    refused(await skill_put(agent, session, "deck-tune"), 409, "room_unbound")
    refused(await skill_delete(agent, session, "deck-tune"), 409, "room_unbound")
    assert (await (await conn.execute("SELECT count(*) FROM room_skills")).fetchone())[0] == 0


async def test_skills_round_trip(agent, conn):
    session = await bound_session(conn)
    r = await skills_list(agent, session)
    assert r.status_code == 200, r.text
    assert r.json() == {"room_id": DM, "skills": [], "max_skills": 20, "max_chars": 8000}
    r = await skill_put(agent, session, "deck-tune", description="tune a deck", body="1. do it")
    assert r.status_code == 200, r.text
    saved = r.json()
    assert (saved["room_id"], saved["name"], saved["description"]) == (
        DM,
        "deck-tune",
        "tune a deck",
    )
    assert (saved["body"], saved["version"], saved["chars"], saved["max_chars"]) == (
        "1. do it",
        1,
        len("1. do it"),
        8000,
    )
    assert saved["updated_at"]
    listing = (await skills_list(agent, session)).json()
    assert listing["skills"] == [
        {
            "name": "deck-tune",
            "description": "tune a deck",
            "version": 1,
            "updated_at": saved["updated_at"],
            "chars": len("1. do it"),
        }
    ]
    assert (await skill_get(agent, session, "deck-tune")).json() == saved
    r = await skill_delete(agent, session, "deck-tune")
    assert r.status_code == 200, r.text
    assert r.json() == {"room_id": DM, "name": "deck-tune", "deleted": True}
    refused(await skill_get(agent, session, "deck-tune"), 404, "skill_not_found")
    refused(await skill_delete(agent, session, "deck-tune"), 404, "skill_not_found")


async def test_skills_isolated_between_rooms(agent, conn):
    a, b = await bound_session(conn, DM), await bound_session(conn, GRP, "hive-2")
    await skill_put(agent, a, "deck-tune", body="A's steps")
    assert (await skills_list(agent, b)).json()["skills"] == []
    refused(await skill_get(agent, b, "deck-tune"), 404, "skill_not_found")
    refused(await skill_delete(agent, b, "deck-tune"), 404, "skill_not_found")
    refused(await skill_put(agent, b, "deck-tune", 1, body="B over A"), 404, "skill_not_found")
    r = await skill_put(agent, b, "deck-tune", body="B's own")
    assert r.status_code == 200 and r.json()["room_id"] == GRP
    rows = await (
        await conn.execute("SELECT room_id, body, version FROM room_skills ORDER BY room_id")
    ).fetchall()
    assert rows == [(DM, "A's steps", 1), (GRP, "B's own", 1)]
    assert (await skill_get(agent, a, "deck-tune")).json()["body"] == "A's steps"


async def test_skill_caps(agent, agent_for, conn):
    session = await bound_session(conn)
    emoji = "\U0001f600"
    assert (await skill_put(agent, session, "big", body=emoji * 8000)).status_code == 200
    r = await skill_put(agent, session, "big", 1, body=emoji * 8001)
    refused(r, 413, "skill_too_large")
    message = r.json()["error"]["message"]
    assert "8000" in message and "8001" in message
    r = await skill_put(agent, session, "desc", description="d" * 1025)
    refused(r, 422, "invalid_request")
    assert (await skill_put(agent, session, "desc", description="d" * 1024)).status_code == 200
    refused(await skill_put(agent, session, "empty", body=""), 422, "invalid_request")
    refused(await skill_put(agent, session, "nodesc", description=""), 422, "invalid_request")

    other = await bound_session(conn, GRP, "hive-2")
    two = agent_for(room_skills_max=2)
    assert (await skill_put(two, other, "one")).status_code == 200
    assert (await skill_put(two, other, "two")).status_code == 200
    r = await skill_put(two, other, "three")
    refused(r, 409, "skill_limit")
    assert r.json()["error"]["message"] == "at most 2 skills per room: delete one first"
    assert (await skill_put(two, other, "two", 1, body="updated")).status_code == 200
    small = agent_for(room_skill_max_chars=100)
    refused(await skill_put(small, other, "two", 2, body="x" * 101), 413, "skill_too_large")
    assert (await skills_list(small, other)).json()["max_chars"] == 100


async def test_skill_count_cap_holds_concurrently(agent_for, conn):
    session = await bound_session(conn)
    one = agent_for(room_skills_max=1)
    results = await asyncio.gather(
        skill_put(one, session, "alpha"), skill_put(one, session, "beta")
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert code(loser) == "skill_limit"
    assert (await (await conn.execute("SELECT count(*) FROM room_skills")).fetchone())[0] == 1


@pytest.mark.parametrize("name", ["Bad", "-x", "a" * 65, "a.b", "a_b", "x y"])
async def test_skill_bad_name_refused(agent, conn, name):
    session = await bound_session(conn)
    refused(await skill_put(agent, session, name), 422, "invalid_request")
    refused(await skill_get(agent, session, name), 422, "invalid_request")
    refused(await skill_delete(agent, session, name), 422, "invalid_request")
    assert (await (await conn.execute("SELECT count(*) FROM room_skills")).fetchone())[0] == 0


async def test_skill_name_limits_accepted(agent, conn):
    session = await bound_session(conn)
    for name in ("a", "0", "a" * 64, "a-b-1"):
        assert (await skill_put(agent, session, name)).status_code == 200, name


async def test_skill_stale_version_conflict(agent, conn):
    session = await bound_session(conn)
    await skill_put(agent, session, "deck-tune", body="one")
    assert (await skill_put(agent, session, "deck-tune", 1, body="two")).status_code == 200
    r = await skill_put(agent, session, "deck-tune", 1, body="three")
    refused(r, 409, "version_conflict")
    assert r.json()["error"]["message"] == (
        "changed since version 1; current version is 2: re-read and merge"
    )
    r = await skill_put(agent, session, "deck-tune", 0, body="again")
    refused(r, 409, "version_conflict")
    got = (await skill_get(agent, session, "deck-tune")).json()
    assert (got["body"], got["version"]) == ("two", 2)


async def test_skill_update_missing_is_404(agent, conn):
    session = await bound_session(conn)
    refused(await skill_put(agent, session, "ghost", 3), 404, "skill_not_found")


async def test_skill_body_never_logged(agent, agent_for, conn, caplog):
    caplog.set_level("DEBUG")
    session = await bound_session(conn)
    text = f"{SKILL_CANARY} payload"
    r = await skill_put(agent, session, "deck-tune", description=text, body=text)
    assert r.status_code == 200
    await skills_list(agent, session)
    await skill_get(agent, session, "deck-tune")
    responses = [
        await skill_put(agent, session, "deck-tune", 0, description=text, body=text),
        await skill_put(agent, session, "deck-tune", 1, description=SKILL_CANARY * 200, body=text),
        await skill_put(
            agent_for(room_skill_max_chars=100), session, "deck-tune", 1, body=text * 20
        ),
        await skill_put(agent, session, "Bad Name", description=text, body=text),
        await skill_put(
            agent_for(room_skills_max=1), session, "other", description=text, body=text
        ),
        await skill_put(agent, session, "ghost", 4, description=text, body=text),
    ]
    assert [r.status_code for r in responses] == [409, 422, 413, 422, 409, 404]
    for response in responses:
        assert SKILL_CANARY not in response.text
    await skill_delete(agent, session, "deck-tune")
    assert SKILL_CANARY not in caplog.text
