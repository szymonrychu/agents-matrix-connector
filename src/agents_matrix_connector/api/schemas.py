"""Request and response models of the UI API (`/matrix/api/v1`) and its error type."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

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


class BindingView(BaseModel):
    """The room's active binding; a reservation by a first run in progress has no session yet."""

    session_id: uuid.UUID | None
    slug: str | None
    origin: Literal["inbound", "agent"]
    since: datetime


class DeniedSender(BaseModel):
    user_id: str
    display_name: str | None
    facebook_id: str | None


class PendingView(BaseModel):
    """Why a room is waiting: not allowed yet, an open invite, a Messenger message request, and
    the messages the policy refused (count and senders, never bodies)."""

    allowed: bool
    invited: bool
    inviter: str | None
    message_request: bool
    denied_count: int
    last_denied_at: datetime | None
    denied_senders: list[DeniedSender]


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
    effective_preset_id: uuid.UUID | None
    """The room's preset, else the default preset."""
    trigger: Literal["all", "mention"] | None
    effective_trigger: Literal["all", "mention"]
    agents_may_initiate: bool
    anonymize_names: bool
    """The chat agent sees pseudonyms (`user-<sha>`, `admin-<sha>`) instead of the people's
    names and ids, and may not send a pseudonym to the room."""
    session: SessionView | None
    binding: BindingView | None
    last_event_at: datetime | None
    queued: int
    pending: PendingView


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
    agents_may_initiate: bool | None = None
    """None (or omitted) keeps the stored value: the flag is an authorisation grant."""
    anonymize_names: bool | None = None
    """None (or omitted) keeps the stored value."""


class RoomAllowIn(BaseModel):
    """As `RoomSettingsIn`: preset and trigger are stored as sent (omitted clears), a null
    `agents_may_initiate` keeps the stored flag."""

    preset_id: uuid.UUID | None = None
    trigger: Literal["all", "mention"] | None = None
    agents_may_initiate: bool | None = None


class RoomAllowView(RoomDetail):
    rule_id: int
    """The room's `allow/room` rule."""
    accept_request_id: int | None
    """The queued accept of the room's Messenger message request or invite, if any."""


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
    preamble: str | None = None


class RuleView(BaseModel):
    id: int
    list: str
    kind: str
    value: str
    note: str | None
    preamble: str | None
    created_at: datetime


class RulesView(BaseModel):
    allow: list[RuleView]
    deny: list[RuleView]


class RulePatch(BaseModel):
    preamble: str | None


class SettingsIn(BaseModel):
    """A field left out keeps its stored value; null clears it."""

    default_preset_id: uuid.UUID | None = None
    preamble: str | None = None


class TemplatePreviewIn(BaseModel):
    template: str
    context: dict[str, Any] | None = None


class TemplatePreviewError(BaseModel):
    line: int | None
    message: str


class TemplatePreviewView(BaseModel):
    rendered: str | None
    error: TemplatePreviewError | None
    variables: list[str]


class SettingsView(BaseModel):
    default_preset_id: uuid.UUID | None
    preamble: str | None
    default_preamble: str
