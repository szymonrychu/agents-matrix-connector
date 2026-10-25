import uuid

import psycopg
import pytest
from psycopg import sql

from agents_matrix_connector import __version__
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
