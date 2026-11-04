"""The bot step for room requests: resolve names through the bridge, execute confirmed rows.

Only the bot holds the Matrix token, so only the bot acts. It never touches a row the user has
not confirmed: `pending_confirmation`, `candidates` and `rejected` rows are left alone. Allowing
a room it opened stays a separate allow call, so `room_settings` and `access_rules` keep the
api as their only writer.
"""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector.bridge import BridgeError, BridgeUnavailable, ProvisioningClient
from agents_matrix_connector.db import directory
from agents_matrix_connector.db import room_requests as repo
from agents_matrix_connector.matrix import (
    MatrixAuthError,
    MatrixClient,
    MatrixError,
    MatrixUnavailable,
)
from agents_matrix_connector.settings import BotSettings

log = logging.getLogger(__name__)

ACCEPT_EVENT = "com.beeper.accept_message_request"
M_DIRECT = "m.direct"
BRIDGE_UNCONFIGURED = "bridge_unconfigured"
NO_MATCH = "no_match"
JOIN_PENDING = "join_pending"
NOT_A_MESSAGE_REQUEST = "not_a_message_request"
ROOM_LEFT = "room_left"
INTERRUPTED = "interrupted"


class _Retry(Exception):
    """Put the row back to `confirmed`; it runs again next tick."""


class _Fail(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RoomRequests:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        matrix: MatrixClient,
        bridge: ProvisioningClient | None,
        settings: BotSettings,
    ):
        self._pool = pool
        self._matrix = matrix
        self._bridge = bridge
        self._settings = settings
        self._recovered = False

    async def tick(self) -> None:
        if not self._recovered:
            await self._recover()
            self._recovered = True
        await self._resolve()
        await self._execute()

    async def _recover(self) -> None:
        """Rows a previous bot left `working` (it stopped mid-call) run again, except a
        Matrix-id open: its room may exist already and a rerun would create a second one."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, action, target_kind FROM room_requests WHERE status = 'working'"
            )
            for request_id, action, kind in await cur.fetchall():
                if action == "open" and kind == "matrix":
                    await repo.fail(conn, request_id, "working", INTERRUPTED)
                    log.info("room request %s failed as interrupted by a restart", request_id)
                elif await repo.release(conn, request_id):
                    log.info("room request %s requeued after a restart", request_id)

    # Resolving names through the bridge search.

    async def _resolve(self) -> None:
        async with self._pool.connection() as conn:
            rows = await repo.claim_resolving(conn)
        for row in rows:
            if self._bridge is None:
                await self._fail(row.id, "resolving", BRIDGE_UNCONFIGURED)
                continue
            try:
                found = await self._bridge.search_users(row.target or "")
            except BridgeUnavailable as exc:
                log.warning("bridge search unavailable for request %s: %s", row.id, exc)
                continue
            except BridgeError as exc:
                await self._fail(row.id, "resolving", f"bridge_error: {exc.errcode}")
                continue
            wanted = (row.target or "").casefold()
            exact = [u for u in found if (u.name or "").casefold() == wanted]
            matches = exact or found
            async with self._pool.connection() as conn:
                if not matches:
                    await repo.fail(conn, row.id, "resolving", NO_MATCH)
                elif len(matches) == 1:
                    await repo.resolved(
                        conn,
                        row.id,
                        target_kind="facebook",
                        target=matches[0].id,
                        display_name=matches[0].name,
                    )
                else:
                    await repo.to_candidates(
                        conn,
                        row.id,
                        [
                            {"user_id": u.mxid, "display_name": u.name, "facebook_id": u.id}
                            for u in matches
                        ],
                    )
            log.info("room request %s resolved to %d match(es)", row.id, len(matches))

    # Executing confirmed rows.

    async def _execute(self) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            rows = await repo.claim_confirmed(conn)
        for row in rows:
            try:
                room_id, reason = await self._run(row)
            except _Retry:
                await self._release(row.id)
                continue
            except _Fail as exc:
                await self._fail(row.id, "working", exc.reason)
                continue
            except BaseException:
                # MatrixAuthError and anything unexpected. A Matrix-id open may have created
                # its room already, so it fails rather than runs twice.
                if row.action == "open" and row.target_kind == "matrix":
                    await self._fail(row.id, "working", INTERRUPTED)
                else:
                    await self._release(row.id)
                raise
            if room_id is None and reason is None:
                await self._release(row.id)  # an accept waiting for its join
                continue
            async with self._pool.connection() as conn:
                await repo.done(conn, row.id, room_id, reason)
            log.info("room request %s done room=%s reason=%s", row.id, room_id, reason)

    async def _run(self, row: repo.RoomRequest) -> tuple[str | None, str | None]:
        """The room and done reason; `(None, None)` keeps the row waiting."""
        try:
            if row.action == "accept":
                return await self._accept(row)
            if row.action == "invite":
                await self._matrix.invite(row.room_id or "", row.target or "")
                return row.room_id, None
            if row.target_kind == "facebook":
                return await self._open_facebook(row)
            return await self._open_matrix(row)
        except BridgeUnavailable as exc:
            log.warning("bridge unavailable for request %s: %s", row.id, exc)
            raise _Retry from None
        except BridgeError as exc:
            raise _Fail(f"bridge_error: {exc.errcode}") from None
        except MatrixAuthError:
            raise
        except MatrixUnavailable as exc:
            log.warning("matrix unavailable for request %s: %s", row.id, exc)
            raise _Retry from None
        except MatrixError as exc:
            raise _Fail(f"matrix_error: {exc.errcode}") from None

    async def _open_facebook(self, row: repo.RoomRequest) -> tuple[str | None, str | None]:
        if self._bridge is None:
            raise _Fail(BRIDGE_UNCONFIGURED)
        user = await self._bridge.create_dm(row.target or "")
        if user.dm_room_mxid is None:
            raise _Fail("bridge_error: no_dm_room")
        try:
            await self._matrix.join(user.dm_room_mxid)
        except MatrixAuthError:
            raise
        except MatrixError as exc:
            # The portal is ours now (a done open row), so the sync joins its invite.
            log.info("join of new portal %s failed, sync joins it: %s", user.dm_room_mxid, exc)
            return user.dm_room_mxid, JOIN_PENDING
        return user.dm_room_mxid, None

    async def _open_matrix(self, row: repo.RoomRequest) -> tuple[str | None, str | None]:
        target = row.target or ""
        room_id = await self._matrix.create_room(invite=[target], is_direct=True)
        try:
            direct = await self._matrix.get_account_data(self._settings.user_id, M_DIRECT) or {}
            rooms = [r for r in direct.get(target, []) if isinstance(r, str)]
            direct[target] = [*rooms, room_id]
            await self._matrix.set_account_data(self._settings.user_id, M_DIRECT, direct)
        except MatrixAuthError:
            raise
        except MatrixError as exc:
            # The room exists; a retry would create a second one. Only the DM marker is lost.
            log.warning("m.direct update for room %s failed: %s", room_id, exc)
        return room_id, None

    async def _accept(self, row: repo.RoomRequest) -> tuple[str | None, str | None]:
        room = await directory.get_room(self._pool, row.room_id or "")
        if room is None or room.membership == "invite":
            return None, None
        if room.membership != "join":
            raise _Fail(ROOM_LEFT)
        if not room.message_request:
            return room.room_id, NOT_A_MESSAGE_REQUEST
        await self._matrix.send_event(room.room_id, ACCEPT_EVENT, f"accept-{row.id}", {})
        return room.room_id, None

    async def _release(self, request_id: int) -> None:
        async with self._pool.connection() as conn:
            await repo.release(conn, request_id)

    async def _fail(self, request_id: int, frm: str, reason: str) -> None:
        async with self._pool.connection() as conn:
            await repo.fail(conn, request_id, frm, reason)
        log.info("room request %s failed: %s", request_id, reason)
