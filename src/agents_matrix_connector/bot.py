"""The bot process: Matrix sync, dispatch worker, agent sender and reply tracker under one
singleton lock."""

import asyncio
import contextlib
import logging
from datetime import timedelta

import httpx
import uvicorn
from agents_common.db.pool import open_pool
from agents_common.orchestrator import OrchestratorClient
from agents_common.runtime import LostLock, advisory_singleton, run_service
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from agents_matrix_connector.matrix import MatrixAuthError, MatrixClient
from agents_matrix_connector.sender import Sender
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.sync import SyncLoop, utcnow
from agents_matrix_connector.tracker import Tracker
from agents_matrix_connector.worker import Worker

EXIT_BAD_SETTINGS = 78
LOCK_NAME = "agents-matrix-connector-bot"
READY_SYNC_INTERVALS = 3

log = logging.getLogger("agents_matrix_connector")


class _Server(uvicorn.Server):
    # uvicorn re-raises a captured signal after a graceful stop; run_service owns the signals.
    def capture_signals(self):
        return contextlib.nullcontext()


async def serve_asgi(app, host: str, port: int, grace_seconds: float, stop: asyncio.Event) -> None:
    """Serve `app` with uvicorn until `stop` is set. Proxy headers stay off."""
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        proxy_headers=False,
        log_config=None,
        timeout_graceful_shutdown=int(grace_seconds),
    )
    server = _Server(config)

    async def watch() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()


def build_health_app(sync: SyncLoop, settings: BotSettings) -> FastAPI:
    """`/healthz` says the process is up; `/readyz` says the sync loop is fresh."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    window = timedelta(seconds=READY_SYNC_INTERVALS * settings.sync_timeout_seconds)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        at = sync.last_sync_at
        if at is None or utcnow() - at > window:
            return JSONResponse({"status": "not ready"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


async def _work_loop(
    worker: Worker, sender: Sender, tracker: Tracker, settings: BotSettings, stop
) -> None:
    """Worker.tick, Sender.tick then Tracker.tick every poll interval; only a Matrix auth
    failure escapes."""
    while not stop.is_set():
        for tick in (worker.tick, sender.tick, tracker.tick):
            try:
                await tick()
            except MatrixAuthError:
                raise
            except Exception:
                log.exception("%s failed; retrying next interval", tick.__self__.__class__.__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.poll_seconds)


async def _locked(
    pool,
    sync: SyncLoop,
    worker: Worker,
    sender: Sender,
    tracker: Tracker,
    settings: BotSettings,
    stop: asyncio.Event,
    held: asyncio.Event,
) -> int:
    health = asyncio.create_task(
        serve_asgi(
            build_health_app(sync, settings),
            settings.http_host,
            settings.http_port,
            settings.shutdown_grace_seconds,
            stop,
        )
    )
    try:
        async with advisory_singleton(pool, LOCK_NAME):
            held.set()
            return await _run_loops(sync, worker, sender, tracker, settings, stop, health)
    finally:
        stop.set()
        await asyncio.gather(health, return_exceptions=True)


async def _run_loops(sync, worker, sender, tracker, settings, stop, health) -> int:
    syncing = asyncio.create_task(sync.run(stop), name="sync")
    working = asyncio.create_task(_work_loop(worker, sender, tracker, settings, stop), name="work")
    stopper = asyncio.create_task(stop.wait())
    tasks = {syncing, working}
    try:
        await asyncio.wait({*tasks, health, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        stopper.cancel()
        syncing.cancel()  # a long poll would outlast the grace period
        results = await asyncio.gather(syncing, working, return_exceptions=True)
    for result in results:
        if isinstance(result, MatrixAuthError):
            log.error("matrix rejected the access token (%s); exiting", result)
            return 1
        if isinstance(result, Exception):
            raise result
    if health.done() and not health.cancelled() and health.exception() is not None:
        raise health.exception()
    return 0


async def run_bot(
    settings: BotSettings,
    *,
    matrix_transport: httpx.AsyncBaseTransport | None = None,
    orchestrator_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Run the bot until SIGTERM. Returns 0 on a clean stop, 78 on a wrong Matrix identity,
    1 when the token is rejected, the singleton lock is lost or the process fails."""
    code = 1

    async def main(stop: asyncio.Event) -> None:
        nonlocal code
        pool = await open_pool(settings.database_url.get_secret_value())
        orchestrator = OrchestratorClient(
            settings.orchestrator_url,
            settings.orchestrator_token.get_secret_value(),
            transport=orchestrator_transport,
        )
        matrix = MatrixClient(
            settings.homeserver_url,
            settings.access_token.get_secret_value(),
            transport=matrix_transport,
        )
        try:
            code = await _main(settings, pool, orchestrator, matrix, stop)
        finally:
            await matrix.aclose()
            await orchestrator.aclose()
            await pool.close()

    exit_code = await run_service(main, grace_seconds=settings.shutdown_grace_seconds)
    return exit_code if exit_code else code


async def _main(settings, pool, orchestrator, matrix, stop) -> int:
    try:
        who = await matrix.whoami()
    except MatrixAuthError as exc:
        log.error("matrix rejected the access token at whoami (%s); exiting", exc)
        return 1
    if who != settings.user_id:
        log.error(
            "access token belongs to %s but MATRIX_USER_ID is %s; refusing to start",
            who,
            settings.user_id,
        )
        return EXIT_BAD_SETTINGS
    sync = SyncLoop(matrix, pool, settings)
    worker = Worker(orchestrator, matrix, pool, settings)
    sender = Sender(pool, settings)
    tracker = Tracker(orchestrator, matrix, pool, settings)
    held = asyncio.Event()
    body = asyncio.create_task(
        _locked(pool, sync, worker, sender, tracker, settings, stop, held), name="bot"
    )
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({body, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set() and not held.is_set() and not body.done():
            body.cancel()  # still waiting for the lock
        try:
            return await body
        except asyncio.CancelledError:
            return 0
        except LostLock as exc:
            log.error("singleton lock lost (%s); exiting", exc)
            return 1
    finally:
        stopper.cancel()
