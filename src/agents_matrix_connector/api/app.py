"""The UI API app (design D11): everything under `/matrix/api/v1`.

It sits behind SSO at the ingress. Errors are `{error: {code, message}}`. Logs never carry an
Authorization header.
"""

import ipaddress
import logging

import psycopg
from agents_common.orchestrator import OrchestratorClient
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_matrix_connector.api.rooms import build_rooms_router
from agents_matrix_connector.api.rules import build_rules_router
from agents_matrix_connector.api.schemas import ApiError
from agents_matrix_connector.api.templates import build_templates_router
from agents_matrix_connector.settings import ApiSettings

log = logging.getLogger(__name__)

PREFIX = "/matrix/api/v1"
EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})


def error_response(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


class SourceAllowlist:
    """403 for a direct socket peer outside the allowlist; X-Forwarded-For is never read.

    The oauth2-proxy sidecar is the only intended caller; its Service port reaches this app
    directly, so without this any in-cluster pod could skip SSO. Probes stay open.
    """

    def __init__(self, app: ASGIApp, networks: list):
        self.app = app
        self.networks = list(networks)

    def allowed(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            addr = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr.version == n.version and addr in n for n in self.networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS or self.allowed(scope):
            await self.app(scope, receive, send)
            return
        await error_response(403, "forbidden", "source address is not allowed")(
            scope, receive, send
        )


def add_error_handlers(app: FastAPI) -> None:
    """Answer every error as `{error: {code, message}}`; shared by the UI and agent apps."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(
            str(part) for part in first.get("loc", ()) if part not in ("body", "query")
        )
        return error_response(
            422, "invalid_request", f"{where}: {first.get('msg', 'invalid request')}"
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "invalid_request"
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(psycopg.Error)
    async def _db(request: Request, exc: psycopg.Error) -> JSONResponse:
        log.error("api: database error (%s)", type(exc).__name__)
        return error_response(503, "unavailable", "database unavailable")


def add_probes(app: FastAPI, pool: AsyncConnectionPool) -> None:
    """`/healthz` says the process is up; `/readyz` says the database answers."""

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        except psycopg.Error as exc:
            log.warning("readyz: database unreachable (%s)", type(exc).__name__)
            return error_response(503, "unavailable", "database unavailable")
        return JSONResponse({"status": "ok"})


def create_api_app(
    pool: AsyncConnectionPool, orchestrator: OrchestratorClient, settings: ApiSettings
) -> FastAPI:
    app = FastAPI(
        title="agents-matrix-connector API",
        openapi_url=f"{PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(build_rooms_router(pool, orchestrator, settings), prefix=PREFIX)
    app.include_router(build_rules_router(pool, orchestrator), prefix=PREFIX)
    app.include_router(build_templates_router(), prefix=PREFIX)
    app.add_middleware(SourceAllowlist, networks=settings.api_networks())
    add_error_handlers(app)
    add_probes(app, pool)
    return app
