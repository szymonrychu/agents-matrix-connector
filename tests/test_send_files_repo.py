"""The send_files queue: one row per file of an agent send (T-155 outbound)."""

import hashlib
import uuid

from agents_matrix_connector.db import send_files, sends

ROOM = "!a:x"


def new_file(position: int, data: bytes, **fields) -> send_files.NewFile:
    values = dict(
        position=position,
        path=f"out/{position}.png",
        name=f"{position}.png",
        mime="image/png",
        msgtype="m.image",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        w=4,
        h=3,
        data=data,
    )
    values.update(fields)
    return send_files.NewFile(**values)


async def put(conn, *, room=ROOM, status="queued", files=(b"one", b"two")) -> int:
    send_id = await sends.insert(
        conn,
        session_id=uuid.uuid4(),
        slug="hive-1",
        kind="message",
        room_id=room,
        text=None,
        key=None,
        reply_to=None,
        ref_event_id=None,
    )
    await send_files.insert_many(conn, send_id, [new_file(n, d) for n, d in enumerate(files)])
    if status != "queued":
        await conn.execute("UPDATE sends SET status = %s WHERE id = %s", (status, send_id))
    return send_id


async def test_insert_get_list_and_content_uri(conn):
    send_id = await put(conn)
    got = await send_files.get(conn, send_id, 1)
    assert got is not None
    row, data = got
    assert data == b"two"
    assert row == send_files.SendFile(
        send_id=send_id,
        position=1,
        path="out/1.png",
        name="1.png",
        mime="image/png",
        msgtype="m.image",
        size=3,
        sha256=hashlib.sha256(b"two").hexdigest(),
        w=4,
        h=3,
        content_uri=None,
    )
    assert await send_files.get(conn, send_id, 2) is None
    await send_files.set_content_uri(conn, send_id, 1, "mxc://x/up1")
    listed = await send_files.list_for_send(conn, send_id)
    assert [(f.position, f.content_uri) for f in listed] == [(0, None), (1, "mxc://x/up1")]
    assert await send_files.list_for_send(conn, send_id + 100) == []


async def test_room_bytes_last_hour(conn):
    assert await send_files.room_bytes_last_hour(conn, ROOM) == 0
    await put(conn, files=(b"a" * 10, b"b" * 5))
    await put(conn, status="sent", files=(b"c" * 7,))
    await put(conn, status="failed", files=(b"d" * 3,))
    await put(conn, status="refused", files=(b"e" * 100,))
    await put(conn, room="!other:x", files=(b"f" * 50,))
    old = await put(conn, files=(b"g" * 1000,))
    await conn.execute(
        "UPDATE sends SET created_at = now() - interval '61 minutes' WHERE id = %s", (old,)
    )
    assert await send_files.room_bytes_last_hour(conn, ROOM) == 25


async def test_clear_settled(conn):
    ids = {status: await put(conn, status=status) for status in ("queued", "sending")}
    ids |= {status: await put(conn, status=status) for status in ("sent", "failed", "refused")}
    assert await send_files.clear_settled(conn) == 6
    for status, send_id in ids.items():
        _, data = await send_files.get(conn, send_id, 0)
        assert (data is None) is (status in ("sent", "failed", "refused")), status
    assert await send_files.clear_settled(conn) == 0
