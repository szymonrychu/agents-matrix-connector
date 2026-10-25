"""Request and response models of the UI API (`/matrix/api/v1`) and its error type."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ApiError(Exception):
    """An error answered as `{error: {code, message}}`."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class AccessView(BaseModel):
    state: Literal["allowed", "denied", "default"]
    reason: str | None
    rule_id: int | None


class SessionView(BaseModel):
    id: uuid.UUID | None
    slug: str | None
    status: str


class RoomView(BaseModel):
    room_id: str
    name: str | None
    alias: str | None
    kind: str | None
    encrypted: bool
    message_request: bool
    membership: str
    member_count: int
    access: AccessView
    preset_id: uuid.UUID | None
    trigger: Literal["all", "mention"] | None
    effective_trigger: Literal["all", "mention"]
    session: SessionView | None
    last_event_at: datetime | None
    queued: int


class MemberView(BaseModel):
    user_id: str
    display_name: str | None
    facebook_id: str | None
    membership: str
    access: AccessView


class RoomDetail(RoomView):
    members: list[MemberView]


class RoomSettingsIn(BaseModel):
    preset_id: uuid.UUID | None = None
    trigger: Literal["all", "mention"] | None = None


class MessageView(BaseModel):
    direction: Literal["in", "out"]
    id: str
    at: datetime
    sender: str
    sender_name: str | None
    body: str | None
    status: str
    reason: str | None
    run_id: int | None


class UserView(BaseModel):
    user_id: str
    display_name: str | None
    facebook_id: str | None
    rooms: list[str]
    access: AccessView


class SyncView(BaseModel):
    last_sync_at: datetime | None
    healthy: bool


class MetaView(BaseModel):
    user_id: str
    homeserver_url: str
    default_policy: Literal["allow", "deny"]
    group_trigger: Literal["all", "mention"]
    rule_kinds: dict[str, list[str]]
    sync: SyncView


class RuleIn(BaseModel):
    list: str
    kind: str
    value: str
    note: str | None = None


class RuleView(BaseModel):
    id: int
    list: str
    kind: str
    value: str
    note: str | None
    created_at: datetime


class RulesView(BaseModel):
    allow: list[RuleView]
    deny: list[RuleView]


class SettingsIn(BaseModel):
    default_preset_id: uuid.UUID | None


class SettingsView(BaseModel):
    default_preset_id: uuid.UUID | None
