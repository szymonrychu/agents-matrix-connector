import asyncio
import hashlib
import secrets
import uuid

import psycopg
import pytest
from psycopg import sql

from agents_matrix_connector import __version__, cli
from agents_matrix_connector.cli import EXIT_BAD_SETTINGS, SUBCOMMANDS, MigrateSettings, main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"agents-matrix-connector {__version__}"


def test_help_exits_0(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_unknown_subcommand_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_no_subcommand_prints_usage_and_exits_2(capsys):
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().out


def test_constants():
    assert EXIT_BAD_SETTINGS == 78
    assert set(SUBCOMMANDS) == {"migrate", "api", "bot"}


MATRIX_VARS = [
    "MATRIX_DATABASE_URL",
    "MATRIX_HOMESERVER_URL",
    "MATRIX_USER_ID",
    "MATRIX_ORCHESTRATOR_URL",
    "MATRIX_ORCHESTRATOR_TOKEN",
    "MATRIX_ORCHESTRATOR_TOKEN_FILE",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_ACCESS_TOKEN_FILE",
]


@pytest.fixture
def clean_env(monkeypatch):
    for name in MATRIX_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_subcommands_and_bad_settings(clean_env, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("migrate", "api", "bot"):
        assert name in out
    clean_env.setenv("MATRIX_DATABASE_URL", "postgresql://u:p@db/x")
    clean_env.setenv("MATRIX_ORCHESTRATOR_URL", "http://orch")
    clean_env.setenv("MATRIX_ORCHESTRATOR_TOKEN", "tok")
    clean_env.setenv("MATRIX_HOMESERVER_URL", "ftp://hs")
    clean_env.setenv("MATRIX_USER_ID", "@agents:x")
    for command in ("api", "bot"):
        assert main([command]) == EXIT_BAD_SETTINGS
        err = capsys.readouterr().err
        assert "MATRIX_HOMESERVER_URL" in err
    clean_env.setenv("MATRIX_HOMESERVER_URL", "http://hs")
    assert main(["bot"]) == EXIT_BAD_SETTINGS
    assert "MATRIX_ACCESS_TOKEN" in capsys.readouterr().err


def test_migrate_needs_only_the_database_url(clean_env, capsys):
    assert main(["migrate"]) == EXIT_BAD_SETTINGS
    err = capsys.readouterr().err
    assert "MATRIX_DATABASE_URL" in err
    assert "ORCHESTRATOR" not in err
    assert "HOMESERVER" not in err


def test_migrate_settings_hold_only_the_database_url(clean_env):
    assert set(MigrateSettings.model_fields) == {"database_url", "log_level"}
    clean_env.setenv("MATRIX_DATABASE_URL", "postgresql://u:p@db/x")
    assert MigrateSettings().database_url.get_secret_value() == "postgresql://u:p@db/x"


def test_migrate_applies_schema(clean_env, pg_url):
    name = f"mxc_cli_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = pg_url.rsplit("/", 1)[0] + "/" + name
    try:
        clean_env.setenv("MATRIX_DATABASE_URL", target)
        assert main(["migrate"]) == 0
        with psycopg.connect(target) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            }
        assert {"rooms", "inbound"} <= tables
    finally:
        with psycopg.connect(pg_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def api_env(clean_env):
    for name in ("MATRIX_AGENT_TOKEN_SHA256", "MATRIX_AGENT_PORT", "MATRIX_HTTP_PORT"):
        clean_env.delenv(name, raising=False)
    clean_env.setenv("MATRIX_DATABASE_URL", "postgresql://u:p@db/x")
    clean_env.setenv("MATRIX_ORCHESTRATOR_URL", "http://orch")
    clean_env.setenv("MATRIX_ORCHESTRATOR_TOKEN", "tok")
    clean_env.setenv("MATRIX_HOMESERVER_URL", "http://hs")
    clean_env.setenv("MATRIX_USER_ID", "@agents:x")
    clean_env.setenv("MATRIX_HTTP_PORT", "9100")
    clean_env.setenv("MATRIX_AGENT_PORT", "9101")
    return clean_env


def fake_run(stop: asyncio.Event):
    """Replace cli._run: run the body with no pool and no orchestrator client."""

    def run(settings, body):
        async def main():
            async with asyncio.timeout(5):
                await body(None, None, stop)

        asyncio.run(main())
        return 0

    return run


def test_api_binds_agent_listener_only_with_hash(api_env):
    served: dict[int, str] = {}

    async def fake_serve(app, host, port, grace_seconds, stop):
        served[port] = app.openapi_url
        await stop.wait()

    async def stop_soon(stop):
        await asyncio.sleep(0.01)
        stop.set()

    api_env.setattr(cli, "serve_asgi", fake_serve)

    def run(settings, body):
        async def main():
            stop = asyncio.Event()
            stopper = asyncio.create_task(stop_soon(stop))
            async with asyncio.timeout(5):
                await body(None, None, stop)
            await stopper

        asyncio.run(main())
        return 0

    api_env.setattr(cli, "_run", run)

    assert main(["api"]) == 0
    assert served == {9100: "/matrix/api/v1/openapi.json"}

    served.clear()
    token = secrets.token_hex(32)
    api_env.setenv("MATRIX_AGENT_TOKEN_SHA256", hashlib.sha256(token.encode()).hexdigest())
    assert main(["api"]) == 0
    assert served == {9100: "/matrix/api/v1/openapi.json", 9101: "/chat/v1/openapi.json"}


def test_api_stops_both_listeners_when_one_exits(api_env):
    token = secrets.token_hex(32)
    api_env.setenv("MATRIX_AGENT_TOKEN_SHA256", hashlib.sha256(token.encode()).hexdigest())
    ended: list[int] = []

    async def fake_serve(app, host, port, grace_seconds, stop):
        try:
            if port == 9101:
                raise OSError("address already in use")
            await stop.wait()
        finally:
            ended.append(port)

    stop = asyncio.Event()
    api_env.setattr(cli, "serve_asgi", fake_serve)
    api_env.setattr(cli, "_run", fake_run(stop))
    with pytest.raises(OSError, match="address already in use"):
        main(["api"])
    assert stop.is_set()
    assert sorted(ended) == [9100, 9101]
