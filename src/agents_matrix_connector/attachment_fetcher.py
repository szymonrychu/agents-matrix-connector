"""The bot's attachment fetcher: delivers the file of a past message an agent asked for.

One queued request per tick: re-check the room binding and the row, download through
`inbound_media.attach` (the current allowlist and size cap), then post the file to the bound
session as a new turn. The Matrix token stays here; bytes live only in memory. Logs carry
ids, statuses and fixed reasons only, never bodies, names, urls or bytes.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime

from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import anonymize, discovery, inbound_media, prompt
from agents_matrix_connector.clock import utcnow
from agents_matrix_connector.db import attachment_requests as requests
from agents_matrix_connector.db import bindings, inbound, rules
from agents_matrix_connector.db.attachment_requests import AttachmentRequest
from agents_matrix_connector.db.inbound import InboundRow
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.settings import BotSettings

log = logging.getLogger(__name__)

ROOM_UNBOUND = "room_unbound"
NO_ATTACHMENT = "no_attachment"
SESSION_GONE = "session_gone"
ORCHESTRATOR_REFUSED = "orchestrator_refused"
ORCHESTRATOR_UNAVAILABLE = "orchestrator_unavailable"
INTERRUPTED = requests.INTERRUPTED


class AttachmentFetcher:
    def __init__(
        self,
        orchestrator: OrchestratorClient,
        matrix: MatrixClient,
        pool: AsyncConnectionPool,
        settings: BotSettings,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._orchestrator = orchestrator
        self._matrix = matrix
        self._pool = pool
        self._settings = settings
        self._now = now
        self._server = settings.user_id.split(":", 1)[1]
        self._puppet = re.compile(settings.puppet_pattern)
        self._started = False

    async def tick(self) -> None:
        if not self._started:
            self._started = True
            async with self._pool.connection() as conn, conn.transaction():
                count = await requests.fail_interrupted(conn)
            if count:
                log.warning("attachment requests interrupted by a restart: %s", count)
        async with self._pool.connection() as conn, conn.transaction():
            request = await requests.claim_next(conn, self._now())
        if request is None:
            return
        row, anonymized = await self._check(request)
        if row is None:
            return
        outcomes = await inbound_media.attach([row], self._matrix, self._settings, self._server)
        outcome = outcomes[0] if outcomes else None
        if outcome is None or outcome.attachment is None:
            await self._finish(request, "failed", outcome.reason if outcome else NO_ATTACHMENT)
            return
        facebook_id = self._facebook_id(row.sender)
        admin = anonymize.is_admin(row.sender, facebook_id, self._settings.anonymize_admins)
        if anonymized:
            sender = anonymize.Labeler(self._settings.anonymize_admins, self._facebook_id).user(
                row.sender
            )
        else:
            sender = prompt.sender_name(row)
        text = prompt.attachment_delivery(request.id, row, outcome, sender, admin, anonymized)
        try:
            message = await self._orchestrator.post_message(
                request.session_id, text, [outcome.attachment]
            )
        except NotFound:
            await self._finish(request, "failed", SESSION_GONE)
        except OrchestratorError:
            await self._finish(request, "failed", ORCHESTRATOR_REFUSED)
        except OrchestratorUnavailable:
            await self._finish(request, "failed", ORCHESTRATOR_UNAVAILABLE)
        else:
            await self._finish(request, "sent", None, str(message.id))

    async def _check(self, request: AttachmentRequest) -> tuple[InboundRow | None, bool]:
        """The row to fetch and whether its room is anonymized; None after a refusal."""
        async with self._pool.connection() as conn:
            binding = await bindings.active_for_room(conn, request.room_id)
            if binding is None or binding.session_id != request.session_id:
                await self._finish(request, "refused", ROOM_UNBOUND, conn=conn)
                return None, False
            row = await inbound.get(conn, request.inbound_id)
            if (
                row is None
                or row.room_id != request.room_id
                or row.redacted
                or row.media_url is None
            ):
                await self._finish(request, "refused", NO_ATTACHMENT, conn=conn)
                return None, False
            setting = await rules.get_room_settings(conn, request.room_id)
        return row, setting is not None and setting.anonymize_names

    async def _finish(
        self,
        request: AttachmentRequest,
        status: str,
        reason: str | None,
        message_id: str | None = None,
        *,
        conn=None,
    ) -> None:
        if conn is not None:
            async with conn.transaction():
                await requests.finish(conn, request.id, status, reason, message_id)
        else:
            async with self._pool.connection() as own, own.transaction():
                await requests.finish(own, request.id, status, reason, message_id)
        log.info("attachment a%s: %s (%s)", request.id, status, reason)

    def _facebook_id(self, user_id: str) -> str | None:
        return discovery.facebook_id(user_id, self._puppet, self._server)
