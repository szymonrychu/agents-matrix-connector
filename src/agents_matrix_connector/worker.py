"""The bot's dispatch worker: queued chat messages into agent sessions (design D8).

A tick first retries every in-progress run that has no message yet (the orchestrator was
unavailable, or the bot restarted mid-dispatch), then handles each room with `new` rows and no
run in progress, oldest first:

1. preset: the room's `room_settings.preset_id`, else the default preset; none ignores the
   rows `no_preset`;
2. reuse: while the room's latest session runs, the batch becomes a `follow` run delivered
   into it (always on for chats, and it bypasses the new-session cap);
3. otherwise, below `MATRIX_MAX_NEW_SESSIONS` first runs in progress, the batch becomes a
   `first` run that creates session `mx-<run id>`; at the cap the rows stay `new` (FIFO).

Creating the run, claiming the room's rows and storing the reused session id happen in one
transaction before any orchestrator call. The calls themselves go through agents-common's
`dispatch` and `deliver`, which skip every step whose id is stored and adopt a posted message
carrying the run's marker, so a retry creates no second session and posts no second prompt.
A delivery whose session is gone fails the run `session_gone` and returns its rows to `new`,
so the next tick starts a new session. The worker never deletes sessions.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from agents_common import runs as agent_runs
from agents_common.db.queue import transition
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import discovery, prompt
from agents_matrix_connector.db import directory, inbound, rules, runs
from agents_matrix_connector.db.inbound import InboundRow
from agents_matrix_connector.matrix import MatrixClient, MatrixError
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.tracker import SESSION_GONE

log = logging.getLogger(__name__)

RUN_KIND = "matrix"
NO_PRESET = "no_preset"
TYPING_TIMEOUT_MS = 30000
UNAVAILABLE_LOG_INTERVAL = timedelta(minutes=1)


def utcnow() -> datetime:
    return datetime.now(UTC)


class _RoomRunStore:
    """The agents-common `RunStore` of a room's run: finishing the run settles its inbound rows
    in the same transaction, so a failed dispatch never leaves rows in progress."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._runs = runs.PgRunStore(pool)

    async def set_session(self, run_id: agent_runs.RunId, session_id: str) -> None:
        await self._runs.set_session(run_id, session_id)

    async def set_message(self, run_id: agent_runs.RunId, message_id: str) -> None:
        await self._runs.set_message(run_id, message_id)

    async def finish(
        self, run_id: agent_runs.RunId, status: agent_runs.RunStatus, reason: str | None
    ) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            if not await runs.finish(conn, int(run_id), status, reason):
                return False
            await inbound.finish_run_rows(conn, int(run_id), status, reason)
        log.info("run %s finished %s (%s)", run_id, status, reason)
        return True


@dataclass(frozen=True)
class _Batch:
    """A run and the inbound rows it carries."""

    room_id: str
    kind: str
    ref: agent_runs.RunRef
    rows: Sequence[InboundRow]


class Worker:
    def __init__(
        self,
        orchestrator: OrchestratorClient,
        matrix: MatrixClient,
        pool: AsyncConnectionPool,
        settings: BotSettings,
        *,
        now: Callable[[], datetime] = utcnow,
    ):
        self._orchestrator = orchestrator
        self._matrix = matrix
        self._pool = pool
        self._settings = settings
        self._now = now
        self._store = _RoomRunStore(pool)
        self._puppet = re.compile(settings.puppet_pattern)
        self._unavailable_logged: dict[str, datetime] = {}

    async def tick(self) -> None:
        async with self._pool.connection() as conn:
            unposted = [run for run in await runs.in_progress(conn) if run.message_id is None]
        for run in unposted:
            await self._resume(run)
        async with self._pool.connection() as conn:
            ready = await inbound.rooms_ready(conn)
        for room_id in ready:
            await self._room(room_id)

    async def _room(self, room_id: str) -> None:
        async with self._pool.connection() as conn:
            preset_id = await self._preset(conn, room_id)
            if preset_id is None:
                ignored = await inbound.ignore_new(conn, room_id, NO_PRESET)
                log.info("room %s has no preset: %s messages ignored", room_id, ignored)
                return
            latest = await runs.latest_session(conn, room_id)
        if latest is not None:
            try:
                session = await agent_runs.running_session(self._orchestrator, str(latest[1]))
            except OrchestratorUnavailable as exc:
                self._unavailable(f"room {room_id}", exc)
                return
            except OrchestratorError as exc:
                log.warning(
                    "session check of room %s refused (%s %s), starting a new session",
                    room_id,
                    exc.status,
                    exc.code,
                )
                session = None
            if session is not None:
                batch = await self._claim(room_id, preset_id, "follow", session.id)
                if batch is not None:
                    await self._deliver(batch)
                return
        batch = await self._claim(room_id, preset_id, "first", None)
        if batch is None:
            log.debug("room %s waits for a new-session slot", room_id)
            return
        await self._dispatch(batch)

    async def _preset(self, conn: psycopg.AsyncConnection, room_id: str) -> uuid.UUID | None:
        room_settings = await rules.get_room_settings(conn, room_id)
        if room_settings is not None and room_settings.preset_id is not None:
            return room_settings.preset_id
        return await rules.get_default_preset(conn)

    async def _claim(
        self, room_id: str, preset_id: uuid.UUID, kind: str, session_id: uuid.UUID | None
    ) -> _Batch | None:
        """Create the room's run and claim its new rows in one transaction; None when a first
        run would pass the new-session cap or the room has no new row left."""
        async with self._pool.connection() as conn, conn.transaction():
            if kind == "first" and (
                await runs.count_first_in_progress(conn) >= self._settings.max_new_sessions
            ):
                return None
            ref = await runs.create_run(conn, room_id=room_id, preset_id=str(preset_id), kind=kind)
            rows = await inbound.claim_room(conn, room_id, int(ref.id))
            if not rows:
                raise psycopg.Rollback()
            if session_id is not None:
                await transition(
                    conn, "runs", ref.id, "in-progress", "in-progress", session_id=session_id
                )
                ref = agent_runs.RunRef(
                    id=ref.id,
                    slug=ref.slug,
                    preset_id=ref.preset_id,
                    session_id=str(session_id),
                    message_id=None,
                    started_at=ref.started_at,
                )
            log.info("run %s (%s) claimed %s messages of room %s", ref.id, kind, len(rows), room_id)
            return _Batch(room_id, kind, ref, rows)
        return None

    async def _resume(self, run: runs.RunRow) -> None:
        """Retry a run whose prompt is not posted yet, with its stored ids."""
        async with self._pool.connection() as conn:
            rows = await inbound.run_rows(conn, run.id)
        ref = agent_runs.RunRef(
            id=run.id,
            slug=run.slug or f"mx-{run.id}",
            preset_id=str(run.preset_id),
            session_id=None if run.session_id is None else str(run.session_id),
            message_id=None,
            started_at=run.started_at,
        )
        batch = _Batch(run.room_id, run.kind, ref, rows)
        if run.kind == "follow":
            await self._deliver(batch)
        else:
            await self._dispatch(batch)

    async def _dispatch(self, batch: _Batch) -> None:
        """Create the run's session `mx-<run id>` and post the first prompt."""
        text = await self._render(batch, first=True)
        try:
            await agent_runs.dispatch(
                self._orchestrator, self._store, batch.ref, text, self._marker(batch)
            )
        except OrchestratorUnavailable as exc:
            self._unavailable(f"run {batch.ref.id}", exc)
            return
        except NotFound as exc:  # dispatch finished the run and its rows
            log.warning("run %s: %s %s", batch.ref.id, exc.status, exc.code)
            return
        except OrchestratorError as exc:
            log.warning("run %s refused: %s %s", batch.ref.id, exc.status, exc.code)
            await self._store.finish(batch.ref.id, "failed", exc.code)
            return
        await self._delivered(batch)

    async def _deliver(self, batch: _Batch) -> None:
        """Post the batch into the room's running session; a gone session sends the rows back
        to `new` for a new session on the next tick."""
        session_id = batch.ref.session_id
        if session_id is None:
            await self._session_gone(batch)
            return
        text = await self._render(batch, first=False)
        try:
            delivered = await agent_runs.deliver(
                self._orchestrator, self._store, batch.ref, session_id, text, self._marker(batch)
            )
        except OrchestratorUnavailable as exc:
            self._unavailable(f"run {batch.ref.id}", exc)
            return
        except OrchestratorError as exc:
            log.warning("run %s refused: %s %s", batch.ref.id, exc.status, exc.code)
            await self._store.finish(batch.ref.id, "failed", exc.code)
            return
        if delivered is None:
            await self._session_gone(batch)
            return
        await self._delivered(batch)

    async def _session_gone(self, batch: _Batch) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            if not await runs.finish(conn, int(batch.ref.id), "failed", SESSION_GONE):
                return
            requeued = await inbound.requeue_run_rows(conn, int(batch.ref.id))
        log.info(
            "run %s: session %s is gone, %s messages back to new",
            batch.ref.id,
            batch.ref.session_id,
            requeued,
        )

    async def _delivered(self, batch: _Batch) -> None:
        self._unavailable_logged.pop(f"run {batch.ref.id}", None)
        self._unavailable_logged.pop(f"room {batch.room_id}", None)
        try:
            await self._matrix.typing(
                self._settings.user_id, batch.room_id, True, TYPING_TIMEOUT_MS
            )
        except MatrixError as exc:
            log.warning("matrix typing in %s failed: %s", batch.room_id, exc)

    async def _render(self, batch: _Batch, *, first: bool) -> str:
        room = await directory.get_room(self._pool, batch.room_id)
        return prompt.render(
            room.name if room else None,
            (room.kind if room else None) or "",
            batch.rows,
            first=first,
            facebook_id=self._facebook_id,
        )

    def _facebook_id(self, user_id: str) -> str | None:
        return discovery.facebook_id(user_id, self._puppet)

    @staticmethod
    def _marker(batch: _Batch) -> str:
        return agent_runs.marker(RUN_KIND, batch.ref.id)

    def _unavailable(self, key: str, exc: Exception) -> None:
        """Log an unavailable orchestrator at most once a minute per run or room."""
        now = self._now()
        last = self._unavailable_logged.get(key)
        if last is None or now - last >= UNAVAILABLE_LOG_INTERVAL:
            self._unavailable_logged[key] = now
            log.warning("%s: orchestrator unavailable, retrying next tick: %s", key, exc)
