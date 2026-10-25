"""The one injectable clock default shared by the worker, tracker and sync loops."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
