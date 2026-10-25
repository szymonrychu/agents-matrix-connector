import pytest

from agents_matrix_connector import __version__
from agents_matrix_connector.cli import EXIT_BAD_SETTINGS, SUBCOMMANDS, main


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
    assert SUBCOMMANDS == {}
