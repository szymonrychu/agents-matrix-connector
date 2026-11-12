"""The bot's reply tracker: agent results back into the Matrix room (design D7, D9, D10).

Each tick polls every in-progress run's orchestrator message. Once the message is sent (the
hive has it), the run's newest inbound message is marked read, once per run and before any
reply of the same tick (`receipts`). A finished run is closed in one transaction that also
settles its inbound rows and stores its outbound rows, whose transaction ids
(`mxc-<run id>-<n>`, `mxc-<run id>-notice`) are deterministic: a crash
between the send and the `sent` update re-sends the same transaction id on the next tick,
and the homeserver deduplicates it, so the room gets the message once. Pending outbound rows,
the runs' and the agent sends' (`mxa-` ids, stored by the sender), are then sent oldest first,
each with its own event type and content; a send's row is sent only while its room still
passes the agent room policy. Once a day old rows are pruned.

In a room flagged `anonymize_names` a final answer containing a pseudonym (`user-<sha>`,
`admin-<sha>`) is never sent nor matched against earlier sends: the run and its rows fail
`pseudonym_disclosed` with no notice in the room, and the run's session gets a message saying
why, so the agent can rephrase and send the answer itself with `orchestrator_chat_send`.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agents_common.orchestrator import (
    Message,
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import anonymize
from agents_matrix_connector.chunks import split
from agents_matrix_connector.clock import utcnow
from agents_matrix_connector.db import bindings, inbound, rules, runs, send_files, sends
from agents_matrix_connector.matrix import (
    MatrixAuthError,
    MatrixClient,
    MatrixError,
    MatrixForbidden,
    MatrixUnavailable,
)
from agents_matrix_connector.receipts import ReadReceipts
from agents_matrix_connector.sender import (
    RoomPolicy,
    event_content,
    file_content,
    refuse_sending,
)
from agents_matrix_connector.settings import BotSettings

log = logging.getLogger(__name__)

SESSION_GONE = "session_gone"
TURN_FAILED = "turn_failed"
TIMEOUT = "timeout"
EMPTY_REPLY = "empty_reply"
ALREADY_SENT = "already_sent"
FORBIDDEN = "forbidden"
MESSAGE_FAILED = "message_failed"
MESSAGE_CANCELLED = "message_cancelled"
TOO_LARGE_FOR_HOMESERVER = "too_large_for_homeserver"
FILE_UNAVAILABLE = "file_unavailable"
UPLOAD_FAILED = "upload_failed"
_RETRY = object()
_MESSAGE_REASONS = {"failed": MESSAGE_FAILED, "cancelled": MESSAGE_CANCELLED}
NOTICE = "Sorry, the agent could not answer that message."
TYPING_TIMEOUT_MS = 30000
PRUNE_INTERVAL = timedelta(days=1)


@dataclass(frozen=True)
class _Outcome:
    """How a finished run closes: its status and reason (shared by its inbound rows), the
    outbound rows it leaves, whether its rows go back to `new` instead, and what its session
    is told once the run is closed."""

    status: str
    reason: str | None
    kind: str = "reply"
    chunks: list[str] = field(default_factory=list)
    requeue: bool = False
    tell_session: str | None = None


_SESSION_GONE = _Outcome("failed", SESSION_GONE, requeue=True)


def _failed(reason: str) -> _Outcome:
    return _Outcome("failed", reason, kind="notice", chunks=[NOTICE])


def _received(message: Message | None) -> bool:
    """The hive has the message: the orchestrator handed it to the agent's wrapper (`sent`), or
    a turn exists for it."""
    return message is not None and (message.status == "sent" or message.turn is not None)


class Tracker:
    def __init__(
        self,
        orchestrator: OrchestratorClient,
        matrix: MatrixClient,
        pool: AsyncConnectionPool,
        settings: BotSettings,
        *,
        now: Callable[[], datetime] = utcnow,
        receipts: ReadReceipts | None = None,
        upload_limit: int | None = None,
    ):
        """`upload_limit` is the homeserver's `m.upload.size`, read once at start; None skips
        the check."""
        self._orchestrator = orchestrator
        self._matrix = matrix
        self._pool = pool
        self._settings = settings
        self._now = now
        self._receipts = receipts or ReadReceipts(matrix)
        self._timeout = timedelta(seconds=settings.run_timeout_seconds)
        self._last_prune: datetime | None = None
        self._upload_limit = upload_limit

    async def tick(self) -> None:
        async with self._pool.connection() as conn:
            active = [run for run in await runs.in_progress(conn) if run.message_id is not None]
        for run in active:
            await self._track(run)
        await self._send_pending()
        await self._prune_daily()

    async def _track(self, run: runs.RunRow) -> None:
        message, outcome = await self._poll(run)
        if _received(message) and not self._receipts.marked(run.id):
            # Before `_finish`, whose reply chunks are sent later in this tick: seen, then reply.
            async with self._pool.connection() as conn:
                rows = await inbound.run_rows(conn, run.id)
            await self._receipts.mark_read(run.id, run.room_id, rows)
        if outcome is None:
            await self._typing(run.room_id, True)
            return
        finished = await self._finish(run, outcome)
        self._receipts.forget(run.id)
        if not finished:
            return
        if not outcome.chunks:
            await self._typing(run.room_id, False)
        if outcome.tell_session is not None:
            await self._tell(run, outcome.tell_session)

    async def _poll(self, run: runs.RunRow) -> tuple[Message | None, _Outcome | None]:
        """The run's polled message (None when not found or the poll failed) and its outcome,
        None while it is still running."""
        assert run.message_id is not None
        try:
            message = await self._orchestrator.get_message(int(run.message_id))
        except NotFound:
            return None, _SESSION_GONE if run.kind == "follow" else _failed(MESSAGE_FAILED)
        except OrchestratorUnavailable as exc:
            log.warning("orchestrator unavailable polling run %s: %s", run.id, exc)
            return None, self._timed_out(run)
        except OrchestratorError as exc:
            # Status and code only: the message text may echo request content.
            log.warning("orchestrator error polling run %s: %s %s", run.id, exc.status, exc.code)
            return None, self._timed_out(run)
        return message, await self._outcome(run, message)

    async def _outcome(self, run: runs.RunRow, message: Message) -> _Outcome | None:
        """The run's outcome from its message, or None while it is still running."""
        turn = message.turn
        if turn is not None and turn.status == "completed":
            chunks = split(turn.final_text or "", self._settings.reply_chunk_chars)
            if not chunks:
                return _Outcome("handled", EMPTY_REPLY)
            rejected = await self._disclosure(run, turn.final_text or "")
            if rejected is not None:
                return rejected
            match = await self._send_match(run, (turn.final_text or "").strip())
            if match == "sent":
                return _Outcome("handled", ALREADY_SENT)
            if match == "pending" and self._now() - run.started_at <= self._timeout:
                return None
            return _Outcome("handled", None, chunks=chunks)
        if turn is not None and turn.status == "failed":
            return _failed(TURN_FAILED)
        if message.status in ("failed", "cancelled"):
            if turn is None and run.kind == "follow":
                return _SESSION_GONE
            return _failed(_MESSAGE_REASONS[message.status])
        return self._timed_out(run)

    async def _disclosure(self, run: runs.RunRow, final_text: str) -> _Outcome | None:
        """The rejection of a final answer that would disclose a pseudonym in a room flagged
        `anonymize_names`, else None. Checked before `_send_match`: a rejected answer is never
        matched as already sent."""
        setting = await rules.get_room_settings(self._pool, run.room_id)
        if setting is None or not setting.anonymize_names:
            return None
        found = anonymize.pseudonyms_in(final_text)
        if not found:
            return None
        log.warning(
            "run %s: final answer refused (%s), %s pseudonym(s) in it",
            run.id,
            anonymize.PSEUDONYM_DISCLOSED,
            len(found),
        )
        return _Outcome(
            "failed",
            anonymize.PSEUDONYM_DISCLOSED,
            tell_session=(
                f"Your final answer for the chat room {run.room_id} was not sent: it "
                f"{anonymize.disclosure_message(found)}. Send the rephrased answer with "
                "orchestrator_chat_send."
            ),
        )

    async def _tell(self, run: runs.RunRow, text: str) -> None:
        """Best effort: post `text` into the run's session; a failure is logged."""
        if run.session_id is None:
            log.warning("run %s: no session to tell why it failed", run.id)
            return
        try:
            await self._orchestrator.post_message(run.session_id, text)
        except OrchestratorUnavailable as exc:
            log.warning("run %s: could not tell session %s why: %s", run.id, run.session_id, exc)
        except OrchestratorError as exc:
            # Status and code only: the message text may echo request content.
            log.warning(
                "run %s: could not tell session %s why: %s %s",
                run.id,
                run.session_id,
                exc.status,
                exc.code,
            )

    async def _send_match(self, run: runs.RunRow, final_text: str) -> str | None:
        """How the final answer was already said (design D10): "sent" when a message send of
        the run's session made since the run started and equal to it (stripped) reached the
        user, else "pending" when one is still queued or sending, else None."""
        if run.session_id is None:
            return None
        async with self._pool.connection() as conn:
            made = await sends.message_sends_since(conn, run.session_id, run.started_at)
        statuses = {status for text, status in made if text.strip() == final_text}
        if "sent" in statuses:
            return "sent"
        return "pending" if statuses else None

    def _timed_out(self, run: runs.RunRow) -> _Outcome | None:
        if self._now() - run.started_at > self._timeout:
            return _failed(TIMEOUT)
        return None

    async def _finish(self, run: runs.RunRow, outcome: _Outcome) -> bool:
        """Close the run, settle its inbound rows and store its outbound rows together;
        False when the run was no longer in progress."""
        async with self._pool.connection() as conn, conn.transaction():
            if not await runs.finish(conn, run.id, outcome.status, outcome.reason):
                return False
            if outcome.requeue:
                await inbound.requeue_run_rows(conn, run.id)
            else:
                await inbound.finish_run_rows(conn, run.id, outcome.status, outcome.reason)
            if outcome.chunks:
                await runs.add_outbound(
                    conn,
                    room_id=run.room_id,
                    run_id=run.id,
                    kind=outcome.kind,
                    chunks=outcome.chunks,
                )
        log.info("run %s finished %s (%s)", run.id, outcome.status, outcome.reason)
        return True

    async def _send_pending(self) -> None:
        """Send pending rows oldest first; an unavailable homeserver stops the pass so later
        chunks never overtake earlier ones, any other refusal fails that row (reason
        `forbidden`, else the errcode) and a lost token propagates. Typing ends once a run
        has no pending row left. A send's row first re-checks its room (D4 (a)); a denied
        room refuses the send and nothing more of it is sent."""
        async with self._pool.connection() as conn:
            pending = await runs.pending(conn)
        left = Counter(row.run_id for row in pending)
        room_policy = RoomPolicy(self._pool, self._settings)
        refused: set[int] = set()
        for row in pending:
            if row.send_id is not None:
                if row.send_id in refused:
                    continue
                refusal = await room_policy.refusal(row.room_id)
                if refusal is not None:
                    refused.add(row.send_id)
                    await self._refuse(row.send_id, refusal)
                    continue
            if row.file_position is not None:
                prepared = await self._file_event(row)
                if prepared is _RETRY:
                    return
                if prepared is None:
                    continue
                content = prepared
            else:
                content = event_content(row.event_type, row.body, row.relates_to)
            try:
                event_id = await self._matrix.send_event(
                    row.room_id, row.event_type, row.txn_id, content
                )
            except MatrixUnavailable as exc:
                log.warning(
                    "matrix unavailable sending %s, retrying next tick: %s", row.txn_id, exc
                )
                return
            except MatrixForbidden as exc:
                log.warning("matrix refused %s in %s: %s", row.txn_id, row.room_id, exc)
                await self._mark_failed(row.id, FORBIDDEN)
            except MatrixAuthError:
                raise
            except MatrixError as exc:
                log.warning("matrix rejected %s in %s: %s", row.txn_id, row.room_id, exc)
                await self._mark_failed(row.id, exc.errcode or "send_failed")
            else:
                async with self._pool.connection() as conn:
                    await runs.mark_sent(conn, row.id, event_id)
            left[row.run_id] -= 1
            if row.run_id is not None and left[row.run_id] == 0:
                await self._typing(row.room_id, False)

    async def _file_event(self, row: runs.OutboundRow) -> Any:
        """The content of a file row, uploading the bytes first unless an earlier tick did
        (`content_uri` is stored, so a retry never uploads twice). None when the row failed
        (`too_large_for_homeserver`, `file_unavailable`, the upload's errcode), `_RETRY` when
        the homeserver is unavailable. Logs name the send and position, never the file."""
        assert row.send_id is not None and row.file_position is not None
        async with self._pool.connection() as conn:
            found = await send_files.get(conn, row.send_id, row.file_position)
        if found is None:
            await self._mark_failed(row.id, FILE_UNAVAILABLE)
            return None
        file, data = found
        if self._upload_limit is not None and file.size > self._upload_limit:
            log.info("send s%s file %s: %s", row.send_id, file.position, TOO_LARGE_FOR_HOMESERVER)
            await self._mark_failed(row.id, TOO_LARGE_FOR_HOMESERVER)
            return None
        uri = file.content_uri
        if uri is None:
            if data is None:
                log.info("send s%s file %s: %s", row.send_id, file.position, FILE_UNAVAILABLE)
                await self._mark_failed(row.id, FILE_UNAVAILABLE)
                return None
            try:
                uri = await self._matrix.upload_media(data, file.mime, file.name)
            except MatrixUnavailable as exc:
                log.warning(
                    "matrix unavailable uploading %s, retrying next tick (%s)",
                    row.txn_id,
                    type(exc).__name__,
                )
                return _RETRY
            except MatrixAuthError:
                raise
            except MatrixError as exc:
                log.warning("matrix rejected the upload of %s: %s", row.txn_id, exc.errcode)
                await self._mark_failed(
                    row.id, FORBIDDEN if exc.status == 403 else exc.errcode or UPLOAD_FAILED
                )
                return None
            async with self._pool.connection() as conn:
                await send_files.set_content_uri(conn, row.send_id, file.position, uri)
            log.info("send s%s file %s uploaded size=%s", row.send_id, file.position, file.size)
        return file_content(file, uri, row.relates_to)

    async def _refuse(self, send_id: int, reason: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await refuse_sending(conn, send_id, reason)
        log.info("send s%s refused at send time: %s", send_id, reason)

    async def _mark_failed(self, outbound_id: int, reason: str) -> None:
        async with self._pool.connection() as conn:
            await runs.mark_failed(conn, outbound_id, reason)

    async def _typing(self, room_id: str, typing: bool) -> None:
        """Typing is cosmetic: a failure is logged, except a lost token, which is fatal."""
        try:
            await self._matrix.typing(self._settings.user_id, room_id, typing, TYPING_TIMEOUT_MS)
        except MatrixAuthError:
            raise
        except MatrixError as exc:
            log.warning("matrix typing %s in %s failed: %s", typing, room_id, exc)

    async def _prune_daily(self) -> None:
        now = self._now()
        if self._last_prune is not None and now < self._last_prune + PRUNE_INTERVAL:
            return
        cutoff = now - timedelta(days=self._settings.retention_days)
        async with self._pool.connection() as conn, conn.transaction():
            inbound_deleted = await inbound.prune(conn, cutoff)
            runs_deleted = await runs.prune(conn, cutoff)
            sends_deleted = await sends.prune(conn, cutoff)
            bindings_deleted = await bindings.prune_released(conn, cutoff)
        self._last_prune = now
        log.info(
            "pruned %s inbound, %s run and outbound, %s send and %s released binding rows"
            " older than %s",
            inbound_deleted,
            runs_deleted,
            sends_deleted,
            bindings_deleted,
            cutoff.isoformat(),
        )
