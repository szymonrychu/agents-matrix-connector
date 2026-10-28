import uuid
from importlib import resources

import psycopg
import pytest
from psycopg import sql

PACKAGE = "agents_matrix_connector"

TABLES = {
    "sync_state",
    "rooms",
    "users",
    "room_members",
    "access_rules",
    "room_settings",
    "settings",
    "inbound",
    "runs",
    "outbound",
    "sends",
    "room_bindings",
}
MIGRATIONS = ["0001_init.sql", "0002_agent_sends.sql"]


@pytest.fixture
def db(migrated_db):
    return migrated_db(PACKAGE)


def _tables(conn):
    return {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        )
    }


def test_migration_idempotent(db):
    from agents_common.db.migrate import run_migrations

    with psycopg.connect(db) as conn:
        assert _tables(conn) >= TABLES
        before = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert run_migrations(db, PACKAGE) == []
    with psycopg.connect(db) as conn:
        after = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert before == after == len(MIGRATIONS)


def test_writer_comments(db):
    expected = {
        "sync_state": "writer: bot sync",
        "rooms": "writer: bot sync",
        "users": "writer: bot sync",
        "room_members": "writer: bot sync",
        "access_rules": "writer: api",
        "room_settings": "writer: api",
        "settings": "writer: api",
        "inbound": "queue: inserted by bot sync, transitions by bot worker",
        "runs": "writer: bot worker",
        "outbound": "writer: bot worker",
        "sends": "queue: inserted by api, transitions by bot worker",
        "room_bindings": "writer: bot worker",
    }
    with psycopg.connect(db) as conn:
        for table, comment in expected.items():
            got = conn.execute("SELECT obj_description(%s::regclass, 'pg_class')", (table,))
            assert got.fetchone()[0].startswith(comment), table
        assert _columns(conn, "sends") == SENDS_COLUMNS
        assert _columns(conn, "room_bindings") == ROOM_BINDINGS_COLUMNS


SENDS_COLUMNS = {
    "id": ("bigint", "NO"),
    "session_id": ("uuid", "NO"),
    "slug": ("text", "YES"),
    "kind": ("text", "NO"),
    "room_id": ("text", "NO"),
    "text": ("text", "YES"),
    "key": ("text", "YES"),
    "reply_to": ("text", "YES"),
    "ref_event_id": ("text", "YES"),
    "event_id": ("text", "YES"),
    "status": ("text", "NO"),
    "reason": ("text", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
    "claimed_at": ("timestamp with time zone", "YES"),
    "sent_at": ("timestamp with time zone", "YES"),
}
ROOM_BINDINGS_COLUMNS = {
    "id": ("bigint", "NO"),
    "room_id": ("text", "NO"),
    "session_id": ("uuid", "YES"),
    "slug": ("text", "YES"),
    "origin": ("text", "NO"),
    "run_id": ("bigint", "YES"),
    "send_id": ("bigint", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
    "released_at": ("timestamp with time zone", "YES"),
    "release_reason": ("text", "YES"),
}


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {name: (data_type, nullable) for name, data_type, nullable in rows}


def _column_default(conn, table, column):
    return conn.execute(
        "SELECT column_default FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()[0]


def _index_columns(conn, table):
    """Each index of the table as (its column list, its partial predicate or None)."""
    rows = conn.execute(
        """
        SELECT ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum, n)
                     JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                     ORDER BY k.n),
               pg_get_expr(i.indpred, i.indrelid), i.indisunique
        FROM pg_index i WHERE i.indrelid = %s::regclass
        """,
        (table,),
    )
    return {(tuple(cols), pred, unique) for cols, pred, unique in rows}


def test_agent_sends_columns(db):
    with psycopg.connect(db) as conn:
        assert _columns(conn, "room_settings")["agents_may_initiate"] == ("boolean", "NO")
        assert _column_default(conn, "room_settings", "agents_may_initiate") == "false"
        outbound = _columns(conn, "outbound")
        assert outbound["send_id"] == ("bigint", "YES")
        assert outbound["event_type"] == ("text", "NO")
        assert outbound["relates_to"] == ("jsonb", "YES")
        assert _column_default(conn, "outbound", "event_type") == "'m.room.message'::text"
        assert _columns(conn, "runs")["preset_id"] == ("uuid", "YES")
        runs_indexes = {cols for cols, _, _ in _index_columns(conn, "runs")}
        assert ("session_id", "room_id") in runs_indexes
        sends_indexes = _index_columns(conn, "sends")
        assert (("id",), "(status = 'queued'::text)", False) in sends_indexes
        assert (("session_id", "created_at"), None, False) in sends_indexes
        assert (("room_id", "status"), None, False) in sends_indexes
        bindings_indexes = _index_columns(conn, "room_bindings")
        assert (("room_id",), "(released_at IS NULL)", True) in bindings_indexes
        assert (
            ("session_id",),
            "((released_at IS NULL) AND (session_id IS NOT NULL))",
            True,
        ) in bindings_indexes

        conn.execute("INSERT INTO room_settings (room_id) VALUES ('!a')")
        assert conn.execute(
            "SELECT agents_may_initiate FROM room_settings WHERE room_id = '!a'"
        ).fetchone() == (False,)
        conn.execute(
            "INSERT INTO outbound (room_id, kind, body, txn_id, status) "
            "VALUES ('!a', 'reply', 'b', 't', 'pending')"
        )
        assert conn.execute(
            "SELECT event_type, send_id, relates_to FROM outbound WHERE txn_id = 't'"
        ).fetchone() == ("m.room.message", None, None)


def test_follow_run_preset_nullable(db):
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runs (room_id, preset_id, kind, status) "
            "VALUES ('!a', NULL, 'follow', 'in-progress')"
        )
        conn.execute(
            "INSERT INTO runs (room_id, preset_id, kind, status) "
            "VALUES ('!a', gen_random_uuid(), 'first', 'in-progress')"
        )
    _rejects(
        db,
        "INSERT INTO runs (room_id, preset_id, kind, status) "
        "VALUES ('!a', NULL, 'first', 'in-progress')",
    )


def _rejects(db, sql, params=()):
    with (
        psycopg.connect(db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(sql, params)


def test_check_constraints(db):
    _rejects(db, "INSERT INTO rooms (room_id, membership) VALUES ('!a', 'banned')")
    _rejects(db, "INSERT INTO rooms (room_id, membership, kind) VALUES ('!a', 'join', 'x')")
    _rejects(db, "INSERT INTO access_rules (list, kind, value) VALUES ('maybe', 'user', 'a')")
    _rejects(db, "INSERT INTO access_rules (list, kind, value) VALUES ('deny', 'other', 'a')")
    _rejects(
        db, "INSERT INTO access_rules (list, kind, value) VALUES ('allow', 'display_name', 'a')"
    )
    _rejects(db, "INSERT INTO access_rules (list, kind, value) VALUES ('deny', 'user', '')")
    _rejects(
        db, "INSERT INTO access_rules (list, kind, value) VALUES ('deny', 'user', %s)", ("x" * 256,)
    )
    _rejects(db, "INSERT INTO room_settings (room_id, trigger) VALUES ('!a', 'sometimes')")
    _rejects(
        db,
        "INSERT INTO inbound (event_id, room_id, sender, status) VALUES ('e', '!a', 's', 'done')",
    )
    _rejects(
        db,
        "INSERT INTO runs (room_id, preset_id, kind, status) "
        "VALUES ('!a', gen_random_uuid(), 'other', 'in-progress')",
    )
    _rejects(
        db,
        "INSERT INTO runs (room_id, preset_id, kind, status) "
        "VALUES ('!a', gen_random_uuid(), 'first', 'new')",
    )
    _rejects(
        db,
        "INSERT INTO outbound (room_id, kind, body, txn_id, status) "
        "VALUES ('!a', 'other', 'b', 't', 'pending')",
    )
    _rejects(
        db,
        "INSERT INTO outbound (room_id, kind, body, txn_id, status) "
        "VALUES ('!a', 'reply', 'b', 't', 'queued')",
    )
    _rejects(
        db,
        "INSERT INTO sends (session_id, kind, room_id, status) "
        "VALUES (gen_random_uuid(), 'other', '!a', 'queued')",
    )
    _rejects(
        db,
        "INSERT INTO sends (session_id, kind, room_id, status) "
        "VALUES (gen_random_uuid(), 'message', '!a', 'done')",
    )
    _rejects(db, "INSERT INTO room_bindings (room_id, origin, run_id) VALUES ('!a', 'other', 1)")
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO access_rules (list, kind, value) VALUES ('deny', 'display_name', 'a')"
        )
        conn.execute("INSERT INTO access_rules (list, kind, value) VALUES ('allow', 'user', 'a')")
        conn.execute("INSERT INTO rooms (room_id, membership) VALUES ('!a', 'invite')")
        for n, kind in enumerate(("reply", "notice", "agent", "reaction")):
            conn.execute(
                "INSERT INTO outbound (room_id, kind, body, txn_id, status) "
                "VALUES ('!a', %s, 'b', %s, 'pending')",
                (kind, f"t{n}"),
            )
        for status in ("queued", "sending", "sent", "failed", "refused"):
            for kind in ("message", "reaction"):
                conn.execute(
                    "INSERT INTO sends (session_id, kind, room_id, status) "
                    "VALUES (gen_random_uuid(), %s, '!a', %s)",
                    (kind, status),
                )


def _unique(db, sql, params=()):
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(sql, params)
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(sql, params)


def test_unique_keys(db):
    _unique(db, "INSERT INTO access_rules (list, kind, value) VALUES ('deny', 'user', 'a')")
    _unique(
        db,
        "INSERT INTO inbound (event_id, room_id, sender, status) VALUES ('e', '!a', 's', 'new')",
    )
    _unique(
        db,
        "INSERT INTO outbound (room_id, kind, body, txn_id, status) "
        "VALUES ('!a', 'reply', 'b', 't', 'pending')",
    )
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("INSERT INTO access_rules (list, kind, value) VALUES ('allow', 'user', 'a')")


@pytest.mark.parametrize("table", ["sync_state", "settings"])
def test_single_row_tables(db, table):
    with psycopg.connect(db, autocommit=True) as conn:
        assert conn.execute(f"SELECT id FROM {table}").fetchall() == [(1,)]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(f"INSERT INTO {table} (id) VALUES (2)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(f"INSERT INTO {table} (id) VALUES (1)")


def _empty_db(pg_url):
    name = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name, f"{pg_url.rsplit('/', 1)[0]}/{name}"


def _migration(name):
    return (resources.files(PACKAGE) / "migrations" / name).read_text()


def test_binding_backfill(pg_url):
    name, url = _empty_db(pg_url)
    s_a, s_old_a, s_b, s_shared, s_gone, s_h = (uuid.uuid4() for _ in range(6))
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(_migration(MIGRATIONS[0]))

            def run(room, kind, status, session, slug):
                return conn.execute(
                    "INSERT INTO runs (room_id, preset_id, kind, status, session_id, slug) "
                    "VALUES (%s, gen_random_uuid(), %s, %s, %s, %s) RETURNING id",
                    (room, kind, status, session, slug),
                ).fetchone()[0]

            # !a: an older session, then its current session with a follow run newest.
            run("!a", "first", "handled", s_old_a, "mx-old-a")
            a_first = run("!a", "first", "handled", s_a, None)
            conn.execute("UPDATE runs SET slug = %s WHERE id = %s", (f"mx-{a_first}", a_first))
            a_follow = run("!a", "follow", "handled", s_a, "mx-follow-a")
            # !b: its session run, then a newer failed follow run without a session.
            b_first = run("!b", "first", "handled", s_b, "mx-b")
            run("!b", "follow", "failed", None, "mx-b-2")
            # !c: an in-progress first run without a session yet, after a gone session.
            run("!c", "first", "handled", s_gone, "mx-gone")
            c_first = run("!c", "first", "in-progress", None, "mx-c")
            # !d and !e share a session: only !e, the room of its newest run, binds it.
            run("!d", "first", "handled", s_shared, "mx-shared")
            e_follow = run("!e", "follow", "in-progress", s_shared, "mx-e")
            # !f: only a failed first run that never got a session.
            run("!f", "first", "failed", None, "mx-f")
            # !h: the session's first run is pruned; a follow run's slug is not its slug.
            h_follow = run("!h", "follow", "handled", s_h, "mx-follow-h")
            # !g: a room with inbound messages and no runs.
            conn.execute(
                "INSERT INTO inbound (event_id, room_id, sender, status) "
                "VALUES ('$g', '!g', '@u:x', 'ignored')"
            )

            conn.execute(_migration(MIGRATIONS[1]))
            rows = conn.execute(
                "SELECT room_id, session_id, slug, origin, run_id, send_id, released_at"
                " FROM room_bindings ORDER BY room_id"
            ).fetchall()
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
    assert rows == [
        ("!a", s_a, f"mx-{a_first}", "inbound", a_follow, None, None),
        ("!b", s_b, "mx-b", "inbound", b_first, None, None),
        ("!c", None, None, "inbound", c_first, None, None),
        ("!e", s_shared, "mx-shared", "inbound", e_follow, None, None),
        ("!h", s_h, None, "inbound", h_follow, None, None),
    ]
