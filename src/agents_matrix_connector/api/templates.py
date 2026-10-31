"""Template preview route: render a preamble template with a sample or given context."""

from __future__ import annotations

import asyncio

from agents_common.templating import preview
from fastapi import APIRouter

from agents_matrix_connector.api.schemas import TemplatePreviewIn, TemplatePreviewView
from agents_matrix_connector.templates import sample_context


def build_templates_router() -> APIRouter:
    router = APIRouter()

    @router.post("/templates/preview", response_model=TemplatePreviewView)
    async def preview_template(body: TemplatePreviewIn) -> dict:
        context = {**sample_context(), **(body.context or {})}
        return await asyncio.to_thread(preview, body.template, context)

    return router
