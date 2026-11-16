import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agents_matrix_connector.db import attachment_requests as repo

ROOM = "!a:x"
SESSION = uuid.uuid4()


async def put(conn, inbound_id=1, room=ROOM, session=SESSION):
    return await repo.insert(
        conn, session_id=session, slug="hive-1", room_id=room, inbound_id=inbound_id
    )


async def status_of(conn, request_id):
    return (await repo.get(conn, request_id)).status


async def test_insert_dedupes_in_flight(conn):
    first = await put(conn)
    assert first is not None
    assert await put(conn) is None
    assert await put(conn, inbound_id=2) is not None
    await repo.claim_next(conn, datetime.now(UTC))
    assert await put(conn) is None  # fetching still counts
    assert await repo.finish(conn, first, "sent", None, "$e")
    assert await put(conn) is not None


async def test_get_and_ref(conn):
    request_id = await put(conn)
    row = await repo.get(conn, request_id)
    assert row == repo.AttachmentRequest(
        id=request_id,
        session_id=SESSION,
        slug="hive-1",
        room_id=ROOM,
        inbound_id=1,
        status="queued",
        reason=None,
        message_id=None,
        created_at=row.created_at,
        claimed_at=None,
        done_at=None,
    )
    assert await repo.get(conn, request_id + 1000) is None
    assert repo.parse_request_ref(f"a{request_id}") == request_id
    for bad in ("a0", "a01", "m1", "a", "a1 ", "a-1", "a9223372036854775808", "a١", ""):
        assert repo.parse_request_ref(bad) is None, bad


async def test_count_recent_counts_every_status(conn):
    ids = [await put(conn, inbound_id=n) for n in range(1, 5)]
    await repo.claim_next(conn, datetime.now(UTC))
    await repo.finish(conn, ids[0], "failed", "x")
    await conn.execute(
        "UPDATE attachment_requests SET created_at = now() - interval '601 seconds' WHERE id = %s",
        (ids[1],),
    )
    await put(conn, inbound_id=9, room="!other:x")
    assert await repo.count_recent(conn, ROOM, 600) == 3
    assert await repo.count_recent(conn, ROOM, 3600) == 4


async def test_claim_next_oldest_first(conn):
    now = datetime.now(UTC)
    assert await repo.claim_next(conn, now) is None
    first, second = await put(conn, 1), await put(conn, 2)
    row = await repo.claim_next(conn, now)
    assert (row.id, row.status, row.claimed_at) == (first, "fetching", now)
    assert (await repo.claim_next(conn, now)).id == second
    assert await repo.claim_next(conn, now) is None


async def test_claim_next_double_claim_loses(pool):
    async with pool.connection() as setup:
        await put(setup)
    gate = asyncio.Event()
    results = []

    async def claimer():
        async with pool.connection() as conn, conn.transaction():
            row = await repo.claim_next(conn, datetime.now(UTC))
            results.append(row)
            await gate.wait()

    first = asyncio.create_task(claimer())
    while not results:
        await asyncio.sleep(0.01)
    second = asyncio.create_task(claimer())
    while len(results) < 2:
        await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(first, second)
    assert sorted(r is not None for r in results) == [False, True]


async def test_finish_is_status_guarded(conn):
    request_id = await put(conn)
    assert await repo.finish(conn, request_id, "sent", None, "$e") is False
    assert await status_of(conn, request_id) == "queued"
    with pytest.raises(ValueError):
        await repo.finish(conn, request_id, "queued", None)
    with pytest.raises(ValueError):
        await repo.finish(conn, request_id, "fetching", None)
    await repo.claim_next(conn, datetime.now(UTC))
    assert await repo.finish(conn, request_id, "sent", None, "$e") is True
    row = await repo.get(conn, request_id)
    assert (row.status, row.message_id, row.done_at is not None) == ("sent", "$e", True)
    assert await repo.finish(conn, request_id, "failed", "late") is False
    assert await status_of(conn, request_id) == "sent"


async def test_fail_interrupted(conn):
    fetching, queued = await put(conn, 1), await put(conn, 2)
    await repo.claim_next(conn, datetime.now(UTC))
    assert await repo.fail_interrupted(conn) == 1
    row = await repo.get(conn, fetching)
    assert (row.status, row.reason, row.done_at is not None) == ("failed", "interrupted", True)
    assert await status_of(conn, queued) == "queued"
    assert await repo.fail_interrupted(conn) == 0


async def test_prune_settled_only(conn):
    ids = [await put(conn, inbound_id=n) for n in range(1, 5)]
    await repo.claim_next(conn, datetime.now(UTC))
    await repo.claim_next(conn, datetime.now(UTC))
    await repo.finish(conn, ids[0], "sent", None)
    await conn.execute("UPDATE attachment_requests SET created_at = now() - interval '40 days'")
    cutoff = datetime.now(UTC) - timedelta(days=30)
    assert await repo.prune(conn, cutoff) == 1
    assert await repo.get(conn, ids[0]) is None
    assert [await status_of(conn, i) for i in ids[1:]] == ["fetching", "queued", "queued"]
