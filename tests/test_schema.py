import psycopg
import pytest

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
}


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
        assert TABLES <= _tables(conn)
        before = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert run_migrations(db, PACKAGE) == []
    with psycopg.connect(db) as conn:
        after = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert before == after == 1


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
    }
    with psycopg.connect(db) as conn:
        for table, comment in expected.items():
            got = conn.execute("SELECT obj_description(%s::regclass, 'pg_class')", (table,))
            assert got.fetchone()[0].startswith(comment), table


def _rejects(db, sql, params=()):
    with psycopg.connect(db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
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
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO access_rules (list, kind, value) VALUES ('deny', 'display_name', 'a')"
        )
        conn.execute("INSERT INTO access_rules (list, kind, value) VALUES ('allow', 'user', 'a')")
        conn.execute("INSERT INTO rooms (room_id, membership) VALUES ('!a', 'invite')")


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
