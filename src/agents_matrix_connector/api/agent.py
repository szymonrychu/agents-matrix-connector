"""The agent listener (design D3): `/chat/v1/*`, `/healthz` and `/readyz` on its own port.

The orchestrator calls it with `Authorization: Bearer <token>`. Only the sha256 hex of the
token is configured (`MATRIX_AGENT_TOKEN_SHA256`) and it is compared in constant time, like the
orchestrator's `api/service_auth.py:ServiceGate`. The token is the only credential: no source
allowlist applies, because the orchestrator pod is outside `MATRIX_API_ALLOWED_CIDRS`. The
token and its hash never reach a log line or an error body. Probes stay open.
"""

import hashlib
import hmac
import logging

from fastapi import APIRouter, FastAPI
from psycopg_pool import AsyncConnectionPool
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_matrix_connector.api.app import (
    EXEMPT_PATHS,
    add_error_handlers,
    add_probes,
    error_response,
)
from agents_matrix_connector.settings import ApiSettings

log = logging.getLogger(__name__)

CHAT_PREFIX = "/chat/v1"
HEALTH_METHODS = frozenset({"GET", "HEAD"})


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            token = token.strip()
            return token if scheme.lower() == "bearer" and token else None
    return None


class AgentGate:
    """401 unless the bearer token's sha256 hex equals `token_sha256`; probes are exempt."""

    def __init__(self, app: ASGIApp, token_sha256: str):
        self.app = app
        self.digest = token_sha256.encode()

    def authenticate(self, token: str) -> bool:
        presented = hashlib.sha256(token.encode()).hexdigest().encode()
        return hmac.compare_digest(presented, self.digest)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope.get("path", "")
        if path in EXEMPT_PATHS and method in HEALTH_METHODS:
            await self.app(scope, receive, send)
            return
        token = _bearer(scope)
        if token is None or not self.authenticate(token):
            log.info("agent request method=%s path=%r status=401", method, path)
            response = error_response(
                401,
                "unauthorized",
                "a valid agent token is required",
                {"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_agent_app(pool: AsyncConnectionPool, settings: ApiSettings) -> FastAPI:
    app = FastAPI(
        title="agents-matrix-connector agent API",
        openapi_url=f"{CHAT_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(APIRouter(prefix=CHAT_PREFIX))
    app.add_middleware(AgentGate, token_sha256=settings.agent_token_sha256.get_secret_value())
    add_error_handlers(app)
    add_probes(app, pool)
    return app
