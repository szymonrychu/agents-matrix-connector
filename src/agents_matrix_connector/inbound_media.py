"""The bot worker's attach step: inbound media rows into orchestrator attachments.

Runs only in the bot pod (the Matrix token lives there). For each row with media, in id order:
media on another server or with a malformed `mxc://` is `foreign_media`, a declared size past
`MATRIX_INBOUND_MEDIA_MAX_BYTES` is `too_large`, and video and audio are `unsupported_type`, all
without a request. Everything else is downloaded (one retry within the tick), then prepared by
`media.prepare_inbound` in a thread under a timeout, and packed first fit under the orchestrator
caps (count and total bytes); rows that no longer fit are `batch_limit`. A failure never stops
the batch: the row is listed to the agent as not attached with its reason. Files are handled one
at a time and their bytes live only in memory. Logs carry row ids and reasons only, never
captions, file names, urls or bytes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from agents_common.orchestrator import Attachment

from agents_matrix_connector import media
from agents_matrix_connector.db.inbound import InboundRow
from agents_matrix_connector.matrix import (
    MatrixAuthError,
    MatrixClient,
    MatrixError,
    MediaTooLarge,
)
from agents_matrix_connector.settings import BotSettings

log = logging.getLogger(__name__)

PREPARE_TIMEOUT_SECONDS = 30
TOO_LARGE = "too_large"
UNSUPPORTED_TYPE = "unsupported_type"
DOWNLOAD_FAILED = "download_failed"
BATCH_LIMIT = "batch_limit"
UNDECODABLE = "undecodable"
FOREIGN_MEDIA = "foreign_media"
_NEVER_DELIVERED = ("video", "audio")


@dataclass(frozen=True)
class Outcome:
    """What became of one row's media: `attachment` when delivered (then `mime` and `size` are
    of the delivered bytes), else None with `reason` (and the declared `mime` and `size`)."""

    row_id: int
    attachment: Attachment | None
    kind: str
    mime: str | None
    size: int | None
    reason: str | None


class _Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def attach(
    rows: Sequence[InboundRow], matrix: MatrixClient, settings: BotSettings, server_name: str
) -> list[Outcome]:
    """One outcome per row with media, in row id order. MatrixAuthError propagates."""
    outcomes: list[Outcome] = []
    count = 0
    total = 0
    for row in sorted(rows, key=lambda r: r.id):
        if row.media_kind is None:
            continue
        try:
            prepared = await _prepare(row, matrix, settings, server_name)
        except _Refused as exc:
            log.info("inbound row %s: media not attached (%s)", row.id, exc.reason)
            outcomes.append(_refused(row, exc.reason))
            continue
        size = len(prepared.data)
        if (
            count >= settings.inbound_attach_max_count
            or total + size > settings.inbound_attach_max_total_bytes
        ):
            log.info("inbound row %s: media not attached (%s)", row.id, BATCH_LIMIT)
            outcomes.append(_refused(row, BATCH_LIMIT))
            continue
        count += 1
        total += size
        attachment = Attachment(f"m{row.id}.{prepared.ext}", prepared.mime, prepared.data)
        outcomes.append(Outcome(row.id, attachment, row.media_kind, prepared.mime, size, None))
    return outcomes


def _refused(row: InboundRow, reason: str) -> Outcome:
    assert row.media_kind is not None
    return Outcome(row.id, None, row.media_kind, row.media_mime, row.media_size, reason)


async def _prepare(
    row: InboundRow, matrix: MatrixClient, settings: BotSettings, server_name: str
) -> media.Prepared:
    location = media.parse_mxc(row.media_url or "", server_name)
    if location is None:
        raise _Refused(FOREIGN_MEDIA)
    if row.media_size is not None and row.media_size > settings.inbound_media_max_bytes:
        raise _Refused(TOO_LARGE)
    if row.media_kind in _NEVER_DELIVERED:
        raise _Refused(UNSUPPORTED_TYPE)
    data = await _download(row, matrix, settings, location)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                media.prepare_inbound,
                data,
                max_edge=settings.inbound_image_max_edge,
                max_pixels=settings.inbound_image_max_pixels,
                max_bytes=settings.inbound_attach_max_bytes,
            ),
            PREPARE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise _Refused(UNDECODABLE) from None
    except media.MediaRefused as exc:
        raise _Refused(exc.reason) from None


async def _download(
    row: InboundRow, matrix: MatrixClient, settings: BotSettings, location: tuple[str, str]
) -> bytes:
    server, media_id = location
    for attempt in range(2):
        try:
            return await matrix.download_media(server, media_id, settings.inbound_media_max_bytes)
        except MediaTooLarge:
            raise _Refused(TOO_LARGE) from None
        except MatrixAuthError:
            raise
        except MatrixError as exc:
            log.warning(
                "inbound row %s: media download failed (attempt %s, status %s)",
                row.id,
                attempt + 1,
                exc.status,
            )
    raise _Refused(DOWNLOAD_FAILED)
