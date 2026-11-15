"""The bot history crawler: a one-time, paced crawl of each bound room's pre-join history."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agents_common.db.pool import open_pool

from agents_matrix_connector import matrix
from agents_matrix_connector.db import inbound, rules
from agents_matrix_connector.db.history_crawls import INTERNAL_ERROR
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.history_crawl import (
    CAP,
    COMPLETE,
    DEFAULT_RETRY_SECONDS,
    MATRIX_ERROR,
    MAX_CRAWL_RETRY_SECONDS,
    NOT_VISIBLE,
    HistoryCrawler,
)
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.sync import SyncLoop

PACKAGE = "agents_matrix_connector"
T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
TOKEN = "syt_crawl_test_token"
BOT = "@hermes:hs"
SELF_FB = "@facebook_999:hs"
BRIDGE = "@facebookbot:hs"
ALICE = "@facebook_1:hs"
BOB = "@facebook_2:hs"
DM = "!dm:hs"


def text(body: str) -> dict:
    return {"msgtype": "m.text", "body": body}


def photo(name: str) -> dict:
    return {
        "msgtype": "m.image",
        "body": "caption",
        "filename": name,
        "url": "mxc://hs/p",
        "info": {"mimetype": "image/jpeg", "size": 1234, "w": 30, "h": 20},
    }


def make_settings(**overrides) -> BotSettings:
    values = dict(
        database_url="postgresql://unused/x",
        orchestrator_url="http://orch:8000",
        orchestrator_token="orch-token",
        homeserver_url="http://hs",
        user_id=BOT,
        self_identities=[SELF_FB],
        bridge_bots=[BRIDGE],
        access_token=TOKEN,
        sync_timeout_seconds=1,
    )
    values.update(overrides)
    return BotSettings(**values)


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def url(migrated_db):
    return migrated_db(PACKAGE)


@pytest.fixture
async def pool(url):
    p = await open_pool(url)
    yield p
    await p.close()


@pytest.fixture
def fake() -> FakeHomeserver:
    return FakeHomeserver(BOT, TOKEN)


@pytest.fixture
async def client(fake):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=fake.app)) as c:
        yield c


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def calls(monkeypatch, client) -> list[tuple]:
    """Every room_messages and event_context call the crawler makes."""
    seen: list[tuple] = []
    messages, context = client.room_messages, client.event_context

    async def spy_messages(room_id, from_token, **kwargs):
        seen.append(("messages", from_token, kwargs["limit"]))
        return await messages(room_id, from_token, **kwargs)

    async def spy_context(room_id, event_id, **kwargs):
        seen.append(("context", event_id))
        return await context(room_id, event_id, **kwargs)

    monkeypatch.setattr(client, "room_messages", spy_messages)
    monkeypatch.setattr(client, "event_context", spy_context)
    return seen


async def fetch(pool, query: str, *args) -> list[tuple]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, args)
        return await cur.fetchall()


async def crawl_state(pool) -> tuple:
    rows = await fetch(
        pool, "SELECT status, reason, events, inserted, not_before FROM history_crawls"
    )
    assert len(rows) == 1
    return rows[0]


async def bind(pool, room_id: str = DM) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO room_bindings (room_id, origin, run_id) VALUES (%s, 'inbound', 1)",
            (room_id,),
        )


def add_prejoin_dm(fake: FakeHomeserver, bodies: list[str], *, sender: str = ALICE) -> list[str]:
    """A DM portal with messages posted before the bot joined; their event ids."""
    fake.add_room(DM, [_bridge_state()], {BRIDGE: "Facebook bridge", ALICE: "Alice"})
    ids = [fake.post(DM, sender, text(body)) for body in bodies]
    fake.set_member(DM, BOT, "join", "hermes")
    return ids


def _bridge_state() -> dict:
    return {
        "type": "m.bridge",
        "state_key": "fb-thread",
        "content": {"com.beeper.room_type.v2": "dm", "channel": {"id": "t", "displayname": "A"}},
    }


async def synced(fake, client, pool, clock, **overrides) -> tuple[SyncLoop, HistoryCrawler]:
    settings = make_settings(**overrides)
    loop = SyncLoop(client, pool, settings, now=clock)
    await loop.run_once()
    return loop, HistoryCrawler(client, pool, loop, settings, now=clock)


async def crawl_all(crawler: HistoryCrawler, pool, ticks: int = 20) -> None:
    for _ in range(ticks):
        await crawler.tick()
        rows = await fetch(pool, "SELECT status FROM history_crawls")
        if rows and rows[0][0] in ("done", "failed"):
            return
    raise AssertionError("crawl did not end")


ROWS = "SELECT body, status, reason, source FROM inbound ORDER BY id"


async def test_prejoin_messages_become_history_rows(fake, client, pool, clock, calls):
    add_prejoin_dm(fake, ["one", "two", "three"])
    await rules.add_rule(pool, "allow", "room", DM, None)
    _, crawler = await synced(fake, client, pool, clock)
    assert await fetch(pool, ROWS) == []
    await bind(pool)

    await crawl_all(crawler, pool)
    rows = await fetch(pool, "SELECT body, status, reason, source FROM inbound ORDER BY origin_ts")
    assert rows == [
        ("one", "ignored", "history", "history"),
        ("two", "ignored", "history", "history"),
        ("three", "ignored", "history", "history"),
    ]
    status, reason, events, inserted, _ = await crawl_state(pool)
    assert (status, reason, events, inserted) == ("done", COMPLETE, 3, 3)
    assert calls[0][0] == "messages" and calls[0][1] is not None
    async with pool.connection() as conn:
        assert await inbound.rooms_ready(conn, T0) == []


async def test_crawl_starts_before_oldest_stored_row(fake, client, pool, clock, calls):
    add_prejoin_dm(fake, ["old one", "old two"])
    await rules.add_rule(pool, "allow", "room", DM, None)
    loop, crawler = await synced(fake, client, pool, clock)
    live = fake.post(DM, ALICE, text("live one"))
    fake.post(DM, ALICE, text("live two"))
    await loop.run_once()
    fake.post(DM, ALICE, text("not yet synced"))
    await bind(pool)

    await crawl_all(crawler, pool)
    assert calls[0] == ("context", live)
    rows = await fetch(pool, "SELECT body, status, source FROM inbound ORDER BY origin_ts")
    assert rows == [
        ("old one", "ignored", "history"),
        ("old two", "ignored", "history"),
        ("live one", "new", "sync"),
        ("live two", "new", "sync"),
    ]


async def test_crawl_without_stored_rows_starts_at_sync_position(fake, client, pool, clock, calls):
    add_prejoin_dm(fake, ["old"])
    await rules.add_rule(pool, "allow", "room", DM, None)
    loop, crawler = await synced(fake, client, pool, clock)
    fake.post(DM, ALICE, text("live after the last sync"))
    await bind(pool)

    await crawl_all(crawler, pool)
    assert [c[0] for c in calls] == ["messages"]
    assert calls[0][1] is not None
    assert await fetch(pool, ROWS) == [("old", "ignored", "history", "history")]

    await loop.run_once()
    assert await fetch(pool, ROWS) == [
        ("old", "ignored", "history", "history"),
        ("live after the last sync", "new", None, "sync"),
    ]
    async with pool.connection() as conn:
        assert await inbound.rooms_ready(conn, T0) == [DM]


async def test_crawl_falls_back_to_sync_position_when_context_fails(
    fake, client, pool, clock, calls
):
    add_prejoin_dm(fake, ["old"])
    await rules.add_rule(pool, "allow", "room", DM, None)
    loop, crawler = await synced(fake, client, pool, clock)
    fake.post(DM, ALICE, text("live"))
    await loop.run_once()
    fake.post(DM, ALICE, text("unsynced"))
    await bind(pool)
    fake.fail_next("room_context", 404)

    await crawl_all(crawler, pool)
    assert [c[0] for c in calls] == ["context", "messages"]
    rows = await fetch(pool, "SELECT body, source FROM inbound ORDER BY origin_ts")
    assert rows == [("old", "history"), ("live", "sync")]


async def test_crawl_continues_past_empty_chunk(fake, client, pool, clock):
    add_prejoin_dm(fake, ["one", "two"])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    fake.empty_page_next()

    await crawler.tick()
    assert (await crawl_state(pool))[0] == "crawling"
    assert await fetch(pool, ROWS) == []
    await crawl_all(crawler, pool)
    assert len(await fetch(pool, ROWS)) == 2
    assert (await crawl_state(pool))[:2] == ("done", COMPLETE)


async def test_crawl_stops_at_cap(fake, client, pool, clock, calls):
    add_prejoin_dm(fake, [f"m{i}" for i in range(300)])
    _, crawler = await synced(fake, client, pool, clock, history_crawl_max_events=150)
    await bind(pool)

    await crawl_all(crawler, pool)
    status, reason, events, inserted, _ = await crawl_state(pool)
    assert (status, reason, events, inserted) == ("done", CAP, 150, 150)
    assert [c[2] for c in calls] == [100, 50]
    bodies = [row[0] for row in await fetch(pool, ROWS)]
    assert sorted(bodies) == sorted(f"m{i}" for i in range(150, 300))


async def test_crawl_complete_when_end_absent(fake, client, pool, clock):
    add_prejoin_dm(fake, ["only"])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    await crawler.tick()
    assert (await crawl_state(pool))[:4] == ("done", COMPLETE, 1, 1)
    await crawler.tick()
    assert (await crawl_state(pool))[:4] == ("done", COMPLETE, 1, 1)


async def test_one_page_per_tick(fake, client, pool, clock, calls):
    add_prejoin_dm(fake, [f"m{i}" for i in range(250)])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    await crawler.tick()
    status, _, events, inserted, _ = await crawl_state(pool)
    assert status == "crawling"
    assert events <= 100 and inserted == events
    assert len(calls) == 1


async def test_crawl_honours_retry_after(fake, client, pool, clock, calls, monkeypatch):
    async def no_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(matrix, "_sleep", no_sleep)
    add_prejoin_dm(fake, ["one"])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    fake.fail_next("room_messages", 429, retry_after_header="120")
    fake.fail_next("room_messages", 429, retry_after_header="120")

    await crawler.tick()
    status, reason, events, _, not_before = await crawl_state(pool)
    # The crawler schedules not_before from the server's Retry-After instead of sleeping.
    assert (status, reason, events) == ("crawling", None, 0)
    assert not_before == T0 + timedelta(seconds=120)

    clock.at = T0 + timedelta(seconds=119)
    await crawler.tick()
    assert len(calls) == 1
    clock.at = T0 + timedelta(seconds=120)
    await crawler.tick()
    assert (await crawl_state(pool))[:4] == ("done", COMPLETE, 1, 1)


async def test_crawl_retry_after_capped_by_crawler_cap(fake, client, pool, clock, monkeypatch):
    async def no_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(matrix, "_sleep", no_sleep)
    add_prejoin_dm(fake, ["one"])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    fake.fail_next("room_messages", 429, retry_after_header="3600")
    fake.fail_next("room_messages", 429, retry_after_header="3600")

    await crawler.tick()
    assert MAX_CRAWL_RETRY_SECONDS == 600
    assert (await crawl_state(pool))[4] == T0 + timedelta(seconds=600)


async def test_crawl_unavailable_without_retry_after_waits_default(fake, client, pool, clock):
    add_prejoin_dm(fake, ["one"])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    fake.fail_next("room_messages", 502)
    await crawler.tick()
    assert (await crawl_state(pool))[4] == T0 + timedelta(seconds=DEFAULT_RETRY_SECONDS)


async def test_crawl_applies_room_policy(fake, client, pool, clock):
    fake.add_room(DM, [_bridge_state()], {BRIDGE: "Facebook bridge", ALICE: "Alice", BOB: "Bob"})
    fake.post(DM, ALICE, text("private words"))
    fake.post(DM, BOB, photo("John_Smith_passport.jpg"))
    fake.set_member(DM, BOT, "join", "hermes")
    await rules.add_rule(pool, "allow", "room", DM, None)
    await rules.add_rule(pool, "deny", "facebook", "1", None)
    await rules.set_room_settings(pool, DM, preset_id=None, trigger=None, anonymize_names=True)
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)

    await crawl_all(crawler, pool)
    rows = await fetch(
        pool,
        "SELECT sender, body, status, reason, media_kind, media_name, source FROM inbound"
        " ORDER BY origin_ts",
    )
    assert rows == [
        (ALICE, None, "ignored", "denied: facebook", None, None, "history"),
        (BOB, "caption", "ignored", "history", "image", None, "history"),
    ]


async def test_crawl_redacted_and_bundled_edit(fake, client, pool, clock):
    add_prejoin_dm(fake, [])
    gone = fake.post(DM, ALICE, text("regretted"))
    fake.redact(DM, gone, ALICE)
    fake.post(DM, ALICE, text("typo"))
    fake._room(DM).timeline[-1][1]["unsigned"]["m.relations"] = {
        "m.replace": {
            "sender": ALICE,
            "origin_server_ts": 1_800_000_000_000,
            "content": {"m.new_content": {"msgtype": "m.text", "body": "bundled"}},
        }
    }
    await rules.add_rule(pool, "allow", "room", DM, None)
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)

    await crawl_all(crawler, pool)
    rows = await fetch(
        pool,
        "SELECT body, status, reason, redacted, edited_at IS NOT NULL, source FROM inbound"
        " ORDER BY origin_ts",
    )
    assert rows == [
        (None, "ignored", "redacted", True, False, "history"),
        ("bundled", "ignored", "history", False, True, "history"),
    ]


async def test_crawl_forbidden_is_not_visible(fake, client, pool, clock):
    add_prejoin_dm(fake, ["one"])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    fake.fail_next("room_messages", 403)
    await crawler.tick()
    assert (await crawl_state(pool))[:2] == ("done", NOT_VISIBLE)
    assert await fetch(pool, ROWS) == []


async def test_crawl_other_matrix_error_fails(fake, client, pool, clock):
    add_prejoin_dm(fake, ["one"])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    fake.fail_next("room_messages", 400)
    await crawler.tick()
    assert (await crawl_state(pool))[:2] == ("failed", MATRIX_ERROR)


async def test_crawl_unexpected_error_fails_and_next_room_proceeds(
    fake, client, pool, clock, monkeypatch
):
    other = "!other:hs"
    add_prejoin_dm(fake, ["one"])
    fake.add_room(other, [_bridge_state()], {BRIDGE: "Facebook bridge", ALICE: "Alice"})
    fake.post(other, ALICE, text("two"))
    fake.set_member(other, BOT, "join", "hermes")
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    await bind(pool, other)
    real = client.room_messages

    async def boom(room_id, from_token, **kwargs):
        if room_id == DM:
            raise RuntimeError("not a matrix error")
        return await real(room_id, from_token, **kwargs)

    monkeypatch.setattr(client, "room_messages", boom)
    for _ in range(6):
        await crawler.tick()
    rows = dict(await fetch(pool, "SELECT room_id, status FROM history_crawls"))
    assert rows == {DM: "failed", other: "done"}
    reasons = dict(await fetch(pool, "SELECT room_id, reason FROM history_crawls"))
    assert reasons[DM] == INTERNAL_ERROR


async def test_crawl_auth_error_propagates(fake, client, pool, clock):
    add_prejoin_dm(fake, ["one"])
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    fake.revoke_token()
    with pytest.raises(matrix.MatrixAuthError):
        await crawler.tick()


async def test_crawler_disabled_with_zero(fake, client, pool, clock, calls):
    add_prejoin_dm(fake, ["one"])
    _, crawler = await synced(fake, client, pool, clock, history_crawl_max_events=0)
    await bind(pool)
    await crawler.tick()
    assert await fetch(pool, "SELECT * FROM history_crawls") == []
    assert calls == []


async def test_crawl_logs_no_bodies(fake, client, pool, clock, caplog):
    caplog.set_level("DEBUG")
    add_prejoin_dm(fake, ["CANARY-crawl-body"])
    await rules.add_rule(pool, "allow", "room", DM, None)
    _, crawler = await synced(fake, client, pool, clock)
    await bind(pool)
    await crawl_all(crawler, pool)
    assert "CANARY-crawl-body" not in caplog.text
    assert TOKEN not in caplog.text
    assert DM in caplog.text
