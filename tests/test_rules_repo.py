import uuid

import psycopg
import pytest
from agents_common.db.pool import open_pool

from agents_matrix_connector.db import rules
from agents_matrix_connector.policy import Rule

PACKAGE = "agents_matrix_connector"


@pytest.fixture
def url(migrated_db):
    return migrated_db(PACKAGE)


@pytest.fixture
async def pool(url):
    p = await open_pool(url)
    yield p
    await p.close()


async def count(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM access_rules")
        return (await cur.fetchone())[0]


async def test_add_and_list_rules(pool, url):
    first = await rules.add_rule(pool, "allow", "user", "  @facebook_1:hs  ", "alice")
    second = await rules.add_rule(pool, "deny", "display_name", "Spam Bot", None)
    assert first == Rule(first.id, "allow", "user", "@facebook_1:hs")
    assert second.id > first.id
    assert await rules.list_rules(pool) == [first, second]

    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        assert await rules.list_rules(conn) == [first, second]

    rows = await rules.list_rule_rows(pool)
    assert [r.id for r in rows] == [first.id, second.id]
    assert rows[0].note == "alice"
    assert rows[1].note is None
    assert rows[0].created_at is not None
    assert (rows[0].list, rows[0].kind, rows[0].value) == ("allow", "user", "@facebook_1:hs")


async def test_duplicate_and_invalid_rules(pool):
    await rules.add_rule(pool, "allow", "user", "@a:hs", None)
    with pytest.raises(rules.RuleExists):
        await rules.add_rule(pool, "allow", "user", " @a:hs ", "again")
    with pytest.raises(ValueError, match="display_name"):
        await rules.add_rule(pool, "allow", "display_name", "Eve", None)
    with pytest.raises(ValueError, match="value"):
        await rules.add_rule(pool, "allow", "user", "not-a-user", None)
    assert await count(pool) == 1


async def test_delete_rule(pool):
    rule = await rules.add_rule(pool, "deny", "room", "!r:hs", None)
    assert await rules.delete_rule(pool, rule.id) is True
    assert await rules.list_rules(pool) == []
    assert await rules.delete_rule(pool, rule.id) is False
    assert await rules.delete_rule(pool, 987654) is False


async def test_room_settings_and_default_preset(pool, url):
    assert await rules.get_room_settings(pool, "!r:hs") is None
    preset = uuid.uuid4()
    saved = await rules.set_room_settings(pool, "!r:hs", preset_id=preset, trigger="mention")
    assert saved == rules.RoomSettings(preset_id=preset, trigger="mention")
    assert await rules.get_room_settings(pool, "!r:hs") == saved
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        assert await rules.get_room_settings(conn, "!r:hs") == saved

    cleared = await rules.set_room_settings(pool, "!r:hs", preset_id=None, trigger=None)
    assert cleared == rules.RoomSettings(preset_id=None, trigger=None)
    assert await rules.get_room_settings(pool, "!r:hs") == cleared

    assert await rules.get_default_preset(pool) is None
    await rules.set_default_preset(pool, preset)
    assert await rules.get_default_preset(pool) == preset
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        assert await rules.get_default_preset(conn) == preset
    await rules.set_default_preset(pool, None)
    assert await rules.get_default_preset(pool) is None
