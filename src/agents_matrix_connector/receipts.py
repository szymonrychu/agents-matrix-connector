"""Read receipts: the bot marks a run's newest message read once the hive has it.

The worker sends the receipt at handoff when the session is already up, the tracker when the
orchestrator reports the run's message sent. One public `m.read` per run in the process: the
run is marked before the send, so a failed send is logged once and not retried each tick. A
restart may resend one receipt per in-flight run, which the bridge ignores (not after LastRead).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from agents_matrix_connector.db.inbound import InboundRow
from agents_matrix_connector.matrix import MatrixClient, MatrixError

log = logging.getLogger(__name__)

_NO_TIME = datetime.min.replace(tzinfo=UTC)


def newest(rows: Sequence[InboundRow]) -> InboundRow:
    """The newest row by origin time (a NULL time sorts lowest), ties to the greatest id."""
    return max(rows, key=lambda row: (row.origin_ts or _NO_TIME, row.id))


class ReadReceipts:
    """The once-per-run read receipt, shared by the worker and the tracker."""

    def __init__(self, matrix: MatrixClient) -> None:
        self._matrix = matrix
        self._marked: set[int] = set()

    def marked(self, run_id: int) -> bool:
        return run_id in self._marked

    async def mark_read(self, run_id: int, room_id: str, rows: Sequence[InboundRow]) -> bool:
        """Send the receipt for the newest of `rows`; False when the run is already marked,
        `rows` is empty or the homeserver refused (logged, not retried)."""
        if run_id in self._marked or not rows:
            return False
        self._marked.add(run_id)
        try:
            await self._matrix.send_receipt(room_id, newest(rows).event_id)
        except MatrixError as exc:
            log.warning("matrix read receipt in %s failed: %s", room_id, exc.status)
            return False
        return True

    def forget(self, run_id: int) -> None:
        self._marked.discard(run_id)
