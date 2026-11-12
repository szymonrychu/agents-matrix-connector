"""Request and response models of the UI API (`/matrix/api/v1`) and its error type."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from agents_matrix_connector.discovery import normalize_aliases


class ApiError(Exception):
    """An error answered as `{error: {code, message}}`."""

    def __init__(self, status: int, code: str, message: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


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
    agent_aliases: list[str]
    """Names people use to call the agent in this room, matched as whole words; the bot's own
    tag and replies to it always count."""
    agents_may_send_files: bool
    """Agents may send workdir files (images, documents) to this room; default false."""
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


class _RoomSettingsBase(BaseModel):
    preset_id: uuid.UUID | None = None
    trigger: Literal["all", "mention"] | None = None
    agents_may_initiate: bool | None = None
    """None (or omitted) keeps the stored value: the flag is an authorisation grant."""
    anonymize_names: bool | None = None
    """None (or omitted) keeps the stored value."""
    agent_aliases: list[str] | None = None
    """None (or omitted) keeps the stored aliases, a list replaces them, `[]` clears them."""

    @field_validator("agent_aliases")
    @classmethod
    def _clean_aliases(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(normalize_aliases(value))


class RoomSettingsIn(_RoomSettingsBase):
    agents_may_send_files: bool | None = None
    """None (or omitted) keeps the stored value: the flag is an authorisation grant."""


class AdminRoomSettingsIn(_RoomSettingsBase):
    """The agent admin body: as `RoomSettingsIn` without `agents_may_send_files`, which only the
    web listener may set (an agent must not grant itself file sends); a request carrying it is
    refused with 422 and a save leaves the stored value unchanged."""

    model_config = ConfigDict(extra="forbid")


class RoomAllowIn(BaseModel):
    """As `RoomSettingsIn`: preset and trigger are stored as sent (omitted clears), a null
    `agents_may_initiate` keeps the stored flag."""

    preset_id: uuid.UUID | None = None
    trigger: Literal["all", "mention"] | None = None
    agents_may_initiate: bool | None = None
    agent_aliases: list[str] | None = None
    """As `RoomSettingsIn.agent_aliases`."""

    @field_validator("agent_aliases")
    @classmethod
    def _clean_aliases(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(normalize_aliases(value))


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
    media_kind: Literal["image", "video", "audio", "file", "sticker"] | None = None
    """An inbound message's attachment kind; never the media url or the original file name."""
    media_mime: str | None = None
    """The declared mime type (sender text)."""
    media_size: int | None = None
    """The declared size in bytes (sender text)."""
    media_status: Literal["attached", "not_attached"] | None = None
    """Whether the media reached the agent; None until the worker handled it."""
    media_reason: str | None = None
    """Why it was not attached: too_large, unsupported_type, download_failed, batch_limit,
    undecodable or foreign_media."""


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
