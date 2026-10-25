"""Command line entrypoint: migrate, api and bot subcommands."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence

from agents_common.db.migrate import run_migrations
from agents_common.db.pool import open_pool
from agents_common.orchestrator import OrchestratorClient
from agents_common.runtime import run_service
from agents_common.settings import BaseServiceSettings, SettingsError, load_settings
from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

from agents_matrix_connector import __version__
from agents_matrix_connector.api.app import create_api_app
from agents_matrix_connector.bot import run_bot, serve_asgi
from agents_matrix_connector.settings import ApiSettings, BotSettings

EXIT_BAD_SETTINGS = 78

SUBCOMMANDS = {
    "migrate": "apply database migrations",
    "api": "run the UI API",
    "bot": "run the Matrix sync, dispatch worker and reply tracker",
}

log = logging.getLogger("agents_matrix_connector")


class MigrateSettings(BaseServiceSettings):
    """Only the database URL is required: migrate needs no orchestrator and no Matrix access."""

    model_config = SettingsConfigDict(env_prefix="MATRIX_", extra="ignore")

    orchestrator_url: str = ""

    @model_validator(mode="after")
    def _token_from_file(self):
        return self


def _load_migrate() -> MigrateSettings:
    try:
        return load_settings(MigrateSettings)
    except SettingsError as exc:
        problems = {k: v for k, v in exc.problems.items() if "ORCHESTRATOR" not in k}
        raise SettingsError(problems) from None


def _run(
    settings: ApiSettings,
    body: Callable[[object, OrchestratorClient, asyncio.Event], Awaitable[None]],
) -> int:
    """Open the pool and orchestrator client, run `body(pool, client, stop)`, close both."""

    async def main(stop: asyncio.Event) -> None:
        pool = await open_pool(settings.database_url.get_secret_value())
        client = OrchestratorClient(
            settings.orchestrator_url, settings.orchestrator_token.get_secret_value()
        )
        try:
            await body(pool, client, stop)
        finally:
            await client.aclose()
            await pool.close()

    return asyncio.run(run_service(main, grace_seconds=settings.shutdown_grace_seconds))


def _cmd_migrate(settings: MigrateSettings) -> int:
    applied = run_migrations(settings.database_url.get_secret_value(), "agents_matrix_connector")
    log.info("migrations applied: %s", ", ".join(applied) or "none")
    return 0


def _cmd_api(settings: ApiSettings) -> int:
    async def body(pool, client, stop):
        app = create_api_app(pool, client, settings)
        await serve_asgi(
            app, settings.http_host, settings.http_port, settings.shutdown_grace_seconds, stop
        )

    return _run(settings, body)


def _cmd_bot(settings: BotSettings) -> int:
    return asyncio.run(run_bot(settings))


COMMANDS: dict[str, tuple[Callable, Callable]] = {
    "migrate": (_load_migrate, _cmd_migrate),
    "api": (lambda: load_settings(ApiSettings), _cmd_api),
    "bot": (lambda: load_settings(BotSettings), _cmd_bot),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents-matrix-connector", description="Agents Matrix connector service"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{" + ",".join(SUBCOMMANDS) + "}")
    for name, help_text in SUBCOMMANDS.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    load, run = COMMANDS[args.command]
    try:
        settings = load()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return EXIT_BAD_SETTINGS
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    return run(settings)


if __name__ == "__main__":
    sys.exit(main())
