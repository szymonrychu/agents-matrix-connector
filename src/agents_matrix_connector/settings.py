"""MATRIX_* settings of the connector."""

import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from agents_common.settings import BaseServiceSettings, CsvList
from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

USER_ID_RE = re.compile(r"^@[^:\s]+:\S+$")


def _var(info: ValidationInfo) -> str:
    return f"MATRIX_{str(info.field_name).upper()}"


class ConnectorSettings(BaseServiceSettings):
    """Environment-driven settings shared by every subcommand."""

    model_config = SettingsConfigDict(env_prefix="MATRIX_", extra="ignore")

    homeserver_url: str
    user_id: str
    device_id: str = "agents-matrix-connector"
    self_identities: CsvList = []
    bridge_bots: CsvList = ["@facebookbot:szymonrichert.pl"]
    puppet_pattern: str = "^@facebook_(?P<id>[0-9]+):"
    default_policy: Literal["deny", "allow"] = "deny"
    group_trigger: Literal["mention", "all"] = "mention"
    max_new_sessions: int = Field(3, ge=1)
    max_queued_per_room: int = Field(20, ge=1)
    max_inbound_chars: int = Field(8000, ge=100)
    reply_chunk_chars: int = Field(4000, ge=100, le=20000)
    run_timeout_seconds: int = Field(1800, ge=60)
    poll_seconds: float = Field(3, gt=0)
    sync_timeout_seconds: int = Field(30, ge=1, le=120)
    retention_days: int = Field(30, ge=1)
    backfill_max_events: int = Field(500, ge=1)
    api_allowed_cidrs: CsvList = ["127.0.0.1/32"]

    @field_validator("homeserver_url")
    @classmethod
    def _homeserver(cls, value: str, info: ValidationInfo) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"{_var(info)}::not an http or https URL")
        return value.rstrip("/")

    @field_validator("user_id")
    @classmethod
    def _user_id(cls, value: str, info: ValidationInfo) -> str:
        if not USER_ID_RE.match(value):
            raise ValueError(f"{_var(info)}::not a Matrix user id (@name:server)")
        return value

    @field_validator("self_identities", "bridge_bots")
    @classmethod
    def _user_ids(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            if not USER_ID_RE.match(item):
                raise ValueError(f"{_var(info)}::not a Matrix user id (@name:server)")
        return value

    @field_validator("puppet_pattern")
    @classmethod
    def _puppet_pattern(cls, value: str, info: ValidationInfo) -> str:
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise ValueError(f"{_var(info)}::not a valid regex: {exc}") from None
        if "id" not in compiled.groupindex:
            raise ValueError(f"{_var(info)}::must contain a named group 'id'")
        return value

    @field_validator("api_allowed_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"{_var(info)}::not a CIDR: {item!r}") from None
        return value

    def self_ids(self) -> frozenset[str]:
        return frozenset([self.user_id, *self.self_identities])

    def api_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.api_allowed_cidrs]


ApiSettings = ConnectorSettings
"""The api never talks to Matrix, so it needs no Matrix access token."""


class BotSettings(ConnectorSettings):
    """Settings of the bot subcommand: adds the Matrix access token."""

    access_token: SecretStr = Field(SecretStr(""), repr=False)
    access_token_file: str = ""

    @model_validator(mode="after")
    def _access_token_from_file(self):
        if self.access_token_file:
            try:
                text = Path(self.access_token_file).read_text().strip()
            except OSError as exc:
                raise ValueError(f"MATRIX_ACCESS_TOKEN_FILE::cannot read: {exc.strerror}") from None
            self.access_token = SecretStr(text)
        if not self.access_token.get_secret_value():
            raise ValueError("MATRIX_ACCESS_TOKEN::required")
        return self
