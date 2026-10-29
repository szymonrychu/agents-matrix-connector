"""MATRIX_* settings of the connector."""

import hashlib
import secrets
from pathlib import Path

import pytest
from agents_common.settings import SettingsError, load_settings

from agents_matrix_connector.settings import ApiSettings, BotSettings, ConnectorSettings

README = Path(__file__).resolve().parent.parent / "README.md"
TOKEN = "syt_super_secret_token_value"

BASE_ENV = {
    "MATRIX_DATABASE_URL": "postgresql://u:p@db/x",
    "MATRIX_ORCHESTRATOR_URL": "http://orch:8000",
    "MATRIX_ORCHESTRATOR_TOKEN": "orch-token",
    "MATRIX_HOMESERVER_URL": "https://matrix.example.org/",
    "MATRIX_USER_ID": "@bot:example.org",
    "MATRIX_ACCESS_TOKEN": TOKEN,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("MATRIX_"):
            monkeypatch.delenv(key)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)


def problems(cls=BotSettings) -> dict[str, str]:
    with pytest.raises(SettingsError) as exc:
        load_settings(cls)
    return exc.value.problems


def test_defaults_and_self_ids(monkeypatch):
    monkeypatch.setenv("MATRIX_SELF_IDENTITIES", "@me:example.org, @me2:example.org")
    s = load_settings(BotSettings)
    assert s.homeserver_url == "https://matrix.example.org"
    assert s.device_id == "agents-matrix-connector"
    assert s.bridge_bots == ["@facebookbot:szymonrichert.pl"]
    assert s.puppet_pattern == "^@facebook_(?P<id>[0-9]+):"
    assert s.default_policy == "deny"
    assert s.group_trigger == "mention"
    assert s.max_new_sessions == 3
    assert s.max_queued_per_room == 20
    assert s.max_inbound_chars == 8000
    assert s.reply_chunk_chars == 4000
    assert s.run_timeout_seconds == 1800
    assert s.poll_seconds == 3
    assert s.sync_timeout_seconds == 30
    assert s.retention_days == 30
    assert s.api_allowed_cidrs == ["127.0.0.1/32"]
    assert s.self_ids() == frozenset({"@bot:example.org", "@me:example.org", "@me2:example.org"})
    assert load_settings(ConnectorSettings).user_id == "@bot:example.org"
    assert ApiSettings is ConnectorSettings


def test_required_variables_named(monkeypatch):
    monkeypatch.delenv("MATRIX_HOMESERVER_URL")
    monkeypatch.delenv("MATRIX_USER_ID")
    assert problems(BotSettings)["MATRIX_HOMESERVER_URL"] == "required"
    assert problems(BotSettings)["MATRIX_USER_ID"] == "required"
    monkeypatch.setenv("MATRIX_HOMESERVER_URL", "http://hs")
    monkeypatch.setenv("MATRIX_USER_ID", "@bot:hs")
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN")
    assert "MATRIX_ACCESS_TOKEN" in problems(BotSettings)
    assert problems(BotSettings)["MATRIX_ACCESS_TOKEN"] == "required"
    # the api needs no Matrix token
    load_settings(ApiSettings)


def test_access_token_file_wins(monkeypatch, tmp_path):
    f = tmp_path / "token"
    f.write_text("  from-file\n")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN_FILE", str(f))
    s = load_settings(BotSettings)
    assert s.access_token.get_secret_value() == "from-file"
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN_FILE", str(tmp_path / "missing"))
    assert "MATRIX_ACCESS_TOKEN_FILE" in problems()
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN")
    f.write_text("")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN_FILE", str(f))
    assert "MATRIX_ACCESS_TOKEN" in problems()


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("MATRIX_HOMESERVER_URL", "ftp://hs"),
        ("MATRIX_HOMESERVER_URL", "hs.example.org"),
        ("MATRIX_USER_ID", "bot:example.org"),
        ("MATRIX_USER_ID", "@bot"),
        ("MATRIX_SELF_IDENTITIES", "@ok:x.org,nope"),
        ("MATRIX_BRIDGE_BOTS", "nope"),
        ("MATRIX_API_ALLOWED_CIDRS", "10.0.0.0/8,banana"),
        ("MATRIX_DEFAULT_POLICY", "maybe"),
        ("MATRIX_GROUP_TRIGGER", "some"),
        ("MATRIX_PUPPET_PATTERN", "^@facebook_([0-9]+):"),
        ("MATRIX_PUPPET_PATTERN", "(unclosed"),
        ("MATRIX_MAX_NEW_SESSIONS", "0"),
        ("MATRIX_MAX_QUEUED_PER_ROOM", "0"),
        ("MATRIX_MAX_INBOUND_CHARS", "99"),
        ("MATRIX_REPLY_CHUNK_CHARS", "99"),
        ("MATRIX_REPLY_CHUNK_CHARS", "20001"),
        ("MATRIX_RUN_TIMEOUT_SECONDS", "59"),
        ("MATRIX_POLL_SECONDS", "0"),
        ("MATRIX_SYNC_TIMEOUT_SECONDS", "0"),
        ("MATRIX_SYNC_TIMEOUT_SECONDS", "121"),
        ("MATRIX_RETENTION_DAYS", "0"),
    ],
)
def test_invalid_values_named(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    assert var in problems()


def test_token_never_rendered(monkeypatch, tmp_path):
    s = load_settings(BotSettings)
    for text in (repr(s), str(s)):
        assert TOKEN not in text
    monkeypatch.setenv("MATRIX_DEFAULT_POLICY", "maybe")
    with pytest.raises(SettingsError) as exc:
        load_settings(BotSettings)
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(exc.value)
    with pytest.raises(Exception) as direct:
        BotSettings()
    assert TOKEN not in str(direct.value)


def test_readme_lists_every_variable():
    text = README.read_text()
    assert "## Configuration" in text
    section = text.split("## Configuration", 1)[1].split("\n## ", 1)[0]
    names = {f"MATRIX_{n.upper()}" for n in BotSettings.model_fields}
    missing = [n for n in sorted(names) if f"`{n}`" not in section]
    assert not missing


def test_agent_listener_settings(monkeypatch):
    s = load_settings(ApiSettings)
    assert s.agent_port == 8001
    assert s.agent_token_sha256.get_secret_value() == ""
    assert s.agent_rate_per_minute == 20
    assert s.agent_room_chunks_per_hour == 60

    digest = hashlib.sha256(secrets.token_hex(32).encode()).hexdigest()
    monkeypatch.setenv("MATRIX_AGENT_TOKEN_SHA256", digest)
    monkeypatch.setenv("MATRIX_AGENT_PORT", "9001")
    monkeypatch.setenv("MATRIX_AGENT_RATE_PER_MINUTE", "1")
    monkeypatch.setenv("MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR", "9")
    s = load_settings(ApiSettings)
    assert s.agent_token_sha256.get_secret_value() == digest
    assert s.agent_port == 9001
    assert s.agent_rate_per_minute == 1
    assert s.agent_room_chunks_per_hour == 9
    for text in (repr(s), str(s), repr(load_settings(BotSettings))):
        assert digest not in text

    for bad in (digest.upper(), digest[:-1], digest + "0", "g" * 64, " " + digest[1:]):
        monkeypatch.setenv("MATRIX_AGENT_TOKEN_SHA256", bad)
        found = problems(ApiSettings)
        assert "MATRIX_AGENT_TOKEN_SHA256" in found
        assert bad.strip() not in str(found)
    monkeypatch.setenv("MATRIX_AGENT_TOKEN_SHA256", digest)

    monkeypatch.setenv("MATRIX_AGENT_RATE_PER_MINUTE", "0")
    assert "MATRIX_AGENT_RATE_PER_MINUTE" in problems(ApiSettings)
    monkeypatch.setenv("MATRIX_AGENT_RATE_PER_MINUTE", "20")
    monkeypatch.setenv("MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR", "0")
    assert "MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR" in problems(ApiSettings)
    monkeypatch.setenv("MATRIX_AGENT_ROOM_CHUNKS_PER_HOUR", "60")

    monkeypatch.setenv("MATRIX_HTTP_PORT", "9001")
    assert "MATRIX_AGENT_PORT" in problems(ApiSettings)
    monkeypatch.delenv("MATRIX_HTTP_PORT")
    monkeypatch.setenv("MATRIX_AGENT_PORT", "8000")
    assert "MATRIX_AGENT_PORT" in problems(ApiSettings)
