"""The bot's dispatch worker: queued chat messages into agent sessions (design D8, D9).

A chat room and a session are bound one to one (`room_bindings`). A tick first retries every
in-progress run that has no message yet (the orchestrator was unavailable, or the bot restarted
mid-dispatch), then, at most once per minute, sweeps the active bindings and releases
(`session_gone`) those whose session the orchestrator reports gone or deleting, then handles
each room with `new` rows and no run in progress, oldest first. The room's active binding
decides, before any preset check:

1. bound to a running session: the batch becomes a `follow` run delivered into it, whatever its
   slug (it bypasses the new-session cap and needs no preset); its `preset_id` is the session's,
   else the room's effective preset, else NULL;
2. bound to a session reported gone or deleting: the binding is released (`session_gone`) and
   the room is handled as unbound in the same tick; a refused session check keeps the binding
   and the rows, and an unavailable orchestrator retries next tick;
3. reserved by a first run in progress: nothing to do;
4. unbound: the room's `room_settings.preset_id`, else the default preset; none ignores the rows
   `no_preset`. Below `MATRIX_MAX_NEW_SESSIONS` first runs in progress, the batch becomes a
   `first` run that creates session `mx-<run id>`; at the cap the rows stay `new` (FIFO).

Creating the run, claiming the room's rows, and storing the follow run's session id or reserving
the room for the first run, happen in one transaction before any orchestrator call. Storing the
first run's session fills the reservation in the same transaction; a first run that ends with no
session releases it (`run_failed`). The calls themselves go through agents-common's `dispatch`
and `deliver`, which skip every step whose id is stored and adopt a posted message carrying the
run's marker, so a retry creates no second session and posts no second prompt. A follow run into
a session that has no other run in the room gets the preamble, like a first prompt. A delivery
whose session is gone fails the run `session_gone`, releases the binding and returns its rows to
`new`, so the next tick starts a new session. The worker never deletes sessions.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import psycopg
from agents_common import runs as agent_runs
from agents_common.db.queue import transition
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
    Session,
)
from agents_common.templating import TemplateError
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import discovery, prompt
from agents_matrix_connector.clock import utcnow
from agents_matrix_connector.db import bindings, directory, inbound, rules, runs
from agents_matrix_connector.db.bindings import BindingRow
from agents_matrix_connector.db.inbound import InboundRow
from agents_matrix_connector.matrix import MatrixClient, MatrixError
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.tracker import SESSION_GONE

log = logging.getLogger(__name__)

RUN_KIND = "matrix"
NO_PRESET = "no_preset"
TYPING_TIMEOUT_MS = 30000
RUN_FAILED = "run_failed"
LOG_INTERVAL = timedelta(minutes=1)
SWEEP_INTERVAL = timedelta(seconds=60)

BindingState = Literal["unbound", "reserved", "bound"]
CheckAction = Literal["follow", "release", "keep", "retry"]
Check = Session | OrchestratorError | OrchestratorUnavailable | None


def binding_state(binding: BindingRow | None) -> BindingState:
    """A room's active binding: none, a first run's reservation (no session yet), or a session."""
    if binding is None:
        return "unbound"
    return "reserved" if binding.session_id is None else "bound"


def check_action(check: Check) -> CheckAction:
    """What the check of a bound session means: a running session takes the batch (`follow`);
    gone or deleting (`running_session` answers None, or not found) releases the binding; an
    unavailable orchestrator retries next tick; any other refusal keeps the binding, which never
    moves on an ambiguous answer."""
    if isinstance(check, Session):
        return "follow"
    if check is None or isinstance(check, NotFound):
        return "release"
    if isinstance(check, OrchestratorUnavailable):
        return "retry"
    return "keep"


class _RoomRunStore:
    """The agents-common `RunStore` of a room's run: storing a first run's session fills the
    room's reservation with it, and finishing a run settles its inbound rows and releases a
    reservation still without a session, each in one transaction, so a failed dispatch never
    leaves rows in progress or a room reserved."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._runs = runs.PgRunStore(pool)

    async def set_session(self, run_id: agent_runs.RunId, session_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            slug = await runs.set_session(conn, int(run_id), session_id)
            if slug is not None and await bindings.fill_session(
                conn, int(run_id), session_id, slug
            ):
                log.info("run %s bound its room to session %s", run_id, session_id)

    async def set_message(self, run_id: agent_runs.RunId, message_id: str) -> None:
        await self._runs.set_message(run_id, message_id)

    async def finish(
        self, run_id: agent_runs.RunId, status: agent_runs.RunStatus, reason: str | None
    ) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            if not await runs.finish(conn, int(run_id), status, reason):
                return False
            await inbound.finish_run_rows(conn, int(run_id), status, reason)
            released = await bindings.release_reservation(conn, int(run_id), RUN_FAILED)
        log.info("run %s finished %s (%s)", run_id, status, reason)
        if released:
            log.info("run %s ended without a session: room reservation released", run_id)
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
        self._bridge_server = settings.user_id.split(":", 1)[1]
        self._logged: dict[str, datetime] = {}
        self._swept_at = now()

    async def tick(self) -> None:
        async with self._pool.connection() as conn:
            unposted = [run for run in await runs.in_progress(conn) if run.message_id is None]
        for run in unposted:
            await self._resume(run)
        await self._sweep()
        async with self._pool.connection() as conn:
            ready = await inbound.rooms_ready(conn)
        for room_id in ready:
            await self._room(room_id)

    async def _sweep(self) -> None:
        """Release every active binding whose session is gone or deleting, at most once per
        `SWEEP_INTERVAL` (the first one interval after start). An unavailable orchestrator ends
        the sweep with nothing released; a refused check keeps that binding."""
        now = self._now()
        if now - self._swept_at < SWEEP_INTERVAL:
            return
        self._swept_at = now
        async with self._pool.connection() as conn:
            active = await bindings.active_with_session(conn)
        gone: list[BindingRow] = []
        for binding in active:
            check = await self._check(binding.session_id)
            action = check_action(check)
            if action == "retry":
                self._unavailable("binding sweep", check)
                return
            if action == "keep":
                self._refused(binding.room_id, check)
            elif action == "release":
                gone.append(binding)
        if not gone:
            return
        async with self._pool.connection() as conn, conn.transaction():
            for binding in gone:
                if await bindings.release(
                    conn, binding.id, SESSION_GONE, session_id=binding.session_id
                ):
                    log.info(
                        "room %s: session %s is gone, binding released",
                        binding.room_id,
                        binding.session_id,
                    )

    async def _room(self, room_id: str) -> None:
        """Route the room's new rows by its active binding, before any preset check."""
        async with self._pool.connection() as conn:
            binding = await bindings.active_for_room(conn, room_id)
        state = binding_state(binding)
        if state == "reserved":
            return
        if state == "bound":
            assert binding is not None and binding.session_id is not None
            check = await self._check(binding.session_id)
            action = check_action(check)
            if action == "retry":
                self._unavailable(f"room {room_id}", check)
                return
            if action == "keep":
                self._refused(room_id, check)
                return
            if action == "follow":
                assert isinstance(check, Session)
                await self._follow(room_id, check)
                return
            async with self._pool.connection() as conn:
                await bindings.release(
                    conn, binding.id, SESSION_GONE, session_id=binding.session_id
                )
            log.info("room %s: session %s is gone, binding released", room_id, binding.session_id)
        await self._unbound(room_id)

    async def _check(self, session_id: uuid.UUID) -> Check:
        """The bound session while it runs, None when gone or deleting, else the error."""
        try:
            return await agent_runs.running_session(self._orchestrator, str(session_id))
        except (OrchestratorError, OrchestratorUnavailable) as exc:
            return exc

    async def _follow(self, room_id: str, session: Session) -> None:
        """Deliver the batch into the room's bound running session."""
        preset_id = session.preset_id
        if preset_id is None:
            async with self._pool.connection() as conn:
                preset_id = await self._preset(conn, room_id)
        batch = await self._claim(room_id, preset_id, "follow", session.id)
        if batch is not None:
            await self._deliver(batch)

    async def _unbound(self, room_id: str) -> None:
        """Start a new `mx-` session from the room's effective preset, which binds the room."""
        async with self._pool.connection() as conn:
            preset_id = await self._preset(conn, room_id)
            if preset_id is None:
                ignored = await inbound.ignore_new(conn, room_id, NO_PRESET)
                log.info("room %s has no preset: %s messages ignored", room_id, ignored)
                return
        batch = await self._claim(room_id, preset_id, "first", None)
        if batch is not None:
            await self._dispatch(batch)

    async def _preset(self, conn: psycopg.AsyncConnection, room_id: str) -> uuid.UUID | None:
        room_settings = await rules.get_room_settings(conn, room_id)
        if room_settings is not None and room_settings.preset_id is not None:
            return room_settings.preset_id
        return await rules.get_default_preset(conn)

    async def _claim(
        self, room_id: str, preset_id: uuid.UUID | None, kind: str, session_id: uuid.UUID | None
    ) -> _Batch | None:
        """Create the room's run and claim its new rows in one transaction, storing a follow
        run's session or reserving the room for a first run; None when a first run would pass
        the new-session cap, the room has no new row left or it got bound meanwhile."""
        async with self._pool.connection() as conn, conn.transaction():
            if kind == "first" and (
                await runs.count_first_in_progress(conn) >= self._settings.max_new_sessions
            ):
                log.debug("room %s waits for a new-session slot", room_id)
                return None
            ref = await runs.create_run(
                conn,
                room_id=room_id,
                preset_id=None if preset_id is None else str(preset_id),
                kind=kind,
            )
            rows = await inbound.claim_room(conn, room_id, int(ref.id))
            if not rows:
                raise psycopg.Rollback()
            if session_id is not None:
                await transition(
                    conn, "runs", ref.id, "in-progress", "in-progress", session_id=session_id
                )
                ref = dataclasses.replace(ref, session_id=str(session_id))
            elif not await bindings.reserve_for_run(conn, room_id, int(ref.id)):
                log.info("room %s got bound meanwhile, retried next tick", room_id)
                raise psycopg.Rollback()
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
            preset_id="" if run.preset_id is None else str(run.preset_id),
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
        """Post the batch into the room's running session, with the preamble when the session
        has no other run in the room (also on a retry through `_resume`); a gone session sends
        the rows back to `new` for a new session on the next tick."""
        session_id = batch.ref.session_id
        if session_id is None:
            await self._session_gone(batch)
            return
        async with self._pool.connection() as conn:
            seen = await runs.has_other_run(conn, session_id, batch.room_id, int(batch.ref.id))
        text = await self._render(batch, first=not seen)
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
            if batch.ref.session_id is not None:
                binding = await bindings.active_for_session(conn, uuid.UUID(batch.ref.session_id))
                if binding is not None:
                    await bindings.release(
                        conn, binding.id, SESSION_GONE, session_id=binding.session_id
                    )
        log.info(
            "run %s: session %s is gone, %s messages back to new",
            batch.ref.id,
            batch.ref.session_id,
            requeued,
        )

    async def _delivered(self, batch: _Batch) -> None:
        for key in (f"run {batch.ref.id}", f"room {batch.room_id}", f"check {batch.room_id}"):
            self._logged.pop(key, None)
        try:
            await self._matrix.typing(
                self._settings.user_id, batch.room_id, True, TYPING_TIMEOUT_MS
            )
        except MatrixError as exc:
            log.warning("matrix typing in %s failed: %s", batch.room_id, exc)
        newest = max(
            batch.rows, key=lambda row: (row.origin_ts or datetime.min.replace(tzinfo=UTC), row.id)
        )
        try:
            await self._matrix.send_receipt(batch.room_id, newest.event_id)
        except MatrixError as exc:
            log.warning("matrix read receipt in %s failed: %s", batch.room_id, exc.status)

    async def _render(self, batch: _Batch, *, first: bool) -> str:
        room = await directory.get_room(self._pool, batch.room_id)
        return prompt.render(
            room.name if room else None,
            (room.kind if room else None) or "",
            batch.rows,
            first=first,
            facebook_id=self._facebook_id,
            preamble=await self._preamble(batch, room.name if room else None) if first else "",
        )

    async def _preamble(self, batch: _Batch, room_name: str | None) -> str:
        """The rendered first-prompt preamble: the deciding rule's template, else the global
        one, else the default; a stored template that no longer renders falls back to the
        default so a bad edit never stalls the chat."""
        rule_id = next((row.rule_id for row in batch.rows if row.rule_id is not None), None)
        rule = None if rule_id is None else await rules.get_rule_preamble(self._pool, rule_id)
        template = (rule[1] if rule else None) or await rules.get_preamble(self._pool)
        if template is None:
            return prompt.DEFAULT_PREAMBLE
        first = batch.rows[0] if batch.rows else None
        context = prompt.preamble_context(
            room_name,
            batch.room_id,
            (first.sender_name or first.sender) if first else "",
            (rule[0] or "") if rule else "",
            self._now(),
        )
        try:
            return await asyncio.to_thread(prompt.render_preamble, template, context)
        except TemplateError as exc:
            log.warning(
                "room %s: preamble does not render, using the default: %s", batch.room_id, exc
            )
            return prompt.DEFAULT_PREAMBLE

    def _facebook_id(self, user_id: str) -> str | None:
        return discovery.facebook_id(user_id, self._puppet, self._bridge_server)

    @staticmethod
    def _marker(batch: _Batch) -> str:
        return agent_runs.marker(RUN_KIND, batch.ref.id)

    def _unavailable(self, key: str, exc: Exception) -> None:
        """Log an unavailable orchestrator at most once a minute per run, room or sweep."""
        if self._due(key):
            log.warning("%s: orchestrator unavailable, retrying next tick: %s", key, exc)

    def _refused(self, room_id: str, exc: Check) -> None:
        """Log a refused session check at most once a minute per room."""
        if isinstance(exc, OrchestratorError) and self._due(f"check {room_id}"):
            log.warning(
                "session check of room %s refused (%s %s), keeping its binding",
                room_id,
                exc.status,
                exc.code,
            )

    def _due(self, key: str) -> bool:
        now = self._now()
        last = self._logged.get(key)
        if last is not None and now - last < LOG_INTERVAL:
            return False
        self._logged[key] = now
        return True
