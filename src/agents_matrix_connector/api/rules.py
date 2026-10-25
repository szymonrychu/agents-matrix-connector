"""Rules, settings and global messages routes (design D6, D11).

Rule values are personal identifiers: they are never logged, only returned to the caller.
"""

from __future__ import annotations

from typing import Literal

from agents_common.orchestrator import NotFound, OrchestratorError, OrchestratorUnavailable
from fastapi import APIRouter, Query, Response
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector.api.rooms import _in_view, _parse_cursor
from agents_matrix_connector.api.schemas import (
    ApiError,
    MessageView,
    RuleIn,
    RulesView,
    RuleView,
    SettingsIn,
    SettingsView,
)
from agents_matrix_connector.db import inbound, rules


def _rule_view(row: rules.RuleRow) -> RuleView:
    return RuleView(
        id=row.id,
        list=row.list,
        kind=row.kind,
        value=row.value,
        note=row.note,
        created_at=row.created_at,
    )


def build_rules_router(pool: AsyncConnectionPool, orchestrator) -> APIRouter:
    router = APIRouter()

    @router.get("/rules", response_model=RulesView)
    async def list_rules() -> RulesView:
        rows = await rules.list_rule_rows(pool)
        return RulesView(
            allow=[_rule_view(r) for r in rows if r.list == "allow"],
            deny=[_rule_view(r) for r in rows if r.list == "deny"],
        )

    @router.post("/rules", response_model=RuleView, status_code=201)
    async def create_rule(body: RuleIn) -> RuleView:
        try:
            rule = await rules.add_rule(pool, body.list, body.kind, body.value, body.note)
        except ValueError as exc:
            raise ApiError(422, "invalid_rule", str(exc)) from None
        except rules.RuleExists as exc:
            raise ApiError(409, "rule_exists", str(exc)) from None
        for row in await rules.list_rule_rows(pool):
            if row.id == rule.id:
                return _rule_view(row)
        raise ApiError(404, "not_found", f"rule {rule.id} not found")

    @router.delete("/rules/{rule_id}", status_code=204)
    async def delete_rule(rule_id: int) -> Response:
        if not await rules.delete_rule(pool, rule_id):
            raise ApiError(404, "not_found", f"rule {rule_id} not found")
        return Response(status_code=204)

    @router.get("/settings", response_model=SettingsView)
    async def get_settings() -> SettingsView:
        return SettingsView(default_preset_id=await rules.get_default_preset(pool))

    @router.put("/settings", response_model=SettingsView)
    async def put_settings(body: SettingsIn) -> SettingsView:
        if body.default_preset_id is not None:
            try:
                await orchestrator.get_preset(body.default_preset_id)
            except NotFound:
                raise ApiError(
                    422,
                    "unknown_preset",
                    f"default_preset_id: unknown preset {body.default_preset_id}",
                ) from None
            except OrchestratorUnavailable:
                raise ApiError(503, "unavailable", "orchestrator unavailable") from None
            except OrchestratorError as exc:
                raise ApiError(502, "orchestrator_error", str(exc)) from None
        await rules.set_default_preset(pool, body.default_preset_id)
        return SettingsView(default_preset_id=body.default_preset_id)

    @router.get("/messages", response_model=list[MessageView])
    async def global_messages(
        status: Literal["new", "in-progress", "handled", "ignored", "failed"] | None = None,
        room_id: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        before_id: str | None = None,
    ) -> list[MessageView]:
        before = None
        if before_id is not None:
            direction, before = _parse_cursor(before_id)
            if direction != "in":
                raise ApiError(422, "invalid_request", "before_id: not an inbound message id")
        rows = await inbound.list_messages(
            pool, room_id=room_id, status=status, limit=limit, before_id=before
        )
        return [_in_view(r) for r in rows]

    return router
