"""E2E fixtures: the real `agents-matrix-connector` console script (migrate, api, bot) as
subprocesses against a Postgres database, with a FakeHomeserver and a FakeOrchestrator each
served by uvicorn on a free port in a thread of the test process.

Each fake runs on its own event loop in its thread; the test thread reaches its state only
through `Served.call`, which runs the helper on that loop, so a `post` wakes a pending long
poll there and no read races a request handler.
"""

import asyncio
import contextlib
import hashlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import psycopg
import pytest
import uvicorn
from agents_common.fake_orchestrator import FakeOrchestrator
from psycopg import sql

from agents_matrix_connector.fake_homeserver import FakeHomeserver

ORCH_TOKEN = "e2e-orchestrator-token"
HS_TOKEN = "e2e-homeserver-token"
SERVER = "e2e.test"
BOT = f"@agents:{SERVER}"
BRIDGE_BOT = f"@facebookbot:{SERVER}"
WAIT_SECONDS = 30.0
API = "/matrix/api/v1"
CHAT = "/chat/v1"
CHUNK_CHARS = 4000
SCRIPT = "agents-matrix-connector"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for[T](probe: Callable[[], T | None], what: str, timeout: float = WAIT_SECONDS) -> T:
    """Poll `probe` until it returns something truthy; fail after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(0.1)


def console_script() -> str:
    found = Path(sys.executable).parent / SCRIPT
    return str(found) if found.exists() else shutil.which(SCRIPT) or SCRIPT


def puppet(facebook_id: str) -> str:
    return f"@facebook_{facebook_id}:{SERVER}"


def bridge_state(room_type: str, displayname: str | None = None) -> dict:
    """The mautrix-meta `m.bridge` state event of a portal of `room_type`."""
    channel: dict[str, Any] = {"id": uuid.uuid4().hex}
    if displayname is not None:
        channel["displayname"] = displayname
    return {
        "type": "m.bridge",
        "state_key": f"fi.mau.meta://facebook/{channel['id']}",
        "sender": BRIDGE_BOT,
        "content": {"com.beeper.room_type.v2": room_type, "channel": channel},
    }


def text(body: str, *, mention: bool = False) -> dict:
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if mention:
        content["m.mentions"] = {"user_ids": [BOT]}
    return content


class Served:
    """An ASGI app served by uvicorn on a free port, on an event loop of its own thread."""

    def __init__(self, app: Any, name: str) -> None:
        self.name = name
        self.port = free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
            )
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _run(self) -> None:
        async def serve() -> None:
            self.loop = asyncio.get_running_loop()
            await self.server.serve()

        asyncio.run(serve())

    def start(self) -> None:
        self.thread.start()
        wait_for(lambda: self.server.started, f"the {self.name} to listen", 15)

    def call[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run `fn(*args, **kwargs)` on the server's loop and return its result."""
        assert self.loop is not None

        async def run() -> T:
            return fn(*args, **kwargs)

        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(10)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


class Stack:
    """One database, both fakes and the api and bot processes, driven through HTTP."""

    def __init__(self, admin_url: str) -> None:
        self.admin_url = admin_url
        self.db_name = f"e2e_{uuid.uuid4().hex}"
        self.hs = FakeHomeserver(BOT, HS_TOKEN)
        self.orch = FakeOrchestrator(ORCH_TOKEN)
        self.hs_server = Served(self.hs.app, "fake homeserver")
        self.orch_server = Served(self.orch.app, "fake orchestrator")
        self.api_port, self.bot_port, self.agent_port = free_port(), free_port(), free_port()
        self.agent_token = secrets.token_hex(32)
        self.procs: dict[str, subprocess.Popen] = {}
        self.http = httpx.Client(timeout=30)
        self.preset_id = ""

    # lifecycle ------------------------------------------------------------------------------

    def _env(self, port: int) -> dict[str, str]:
        base = self.admin_url.rsplit("/", 1)[0]
        return {
            **os.environ,
            "MATRIX_DATABASE_URL": f"{base}/{self.db_name}",
            "MATRIX_ORCHESTRATOR_URL": self.orch_server.url,
            "MATRIX_ORCHESTRATOR_TOKEN": ORCH_TOKEN,
            "MATRIX_HOMESERVER_URL": self.hs_server.url,
            "MATRIX_USER_ID": BOT,
            "MATRIX_ACCESS_TOKEN": HS_TOKEN,
            "MATRIX_BRIDGE_BOTS": BRIDGE_BOT,
            "MATRIX_HTTP_HOST": "127.0.0.1",
            "MATRIX_HTTP_PORT": str(port),
            "MATRIX_AGENT_PORT": str(self.agent_port),
            "MATRIX_AGENT_TOKEN_SHA256": hashlib.sha256(self.agent_token.encode()).hexdigest(),
            "MATRIX_REPLY_CHUNK_CHARS": str(CHUNK_CHARS),
            "MATRIX_POLL_SECONDS": "0.2",
            "MATRIX_SYNC_TIMEOUT_SECONDS": "1",
            "MATRIX_SHUTDOWN_GRACE_SECONDS": "5",
        }

    def _spawn(self, command: str, port: int) -> subprocess.Popen:
        return subprocess.Popen(
            [console_script(), command],
            env=self._env(port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def start(self) -> None:
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.db_name)))
        self.hs_server.start()
        self.orch_server.start()
        self.preset_id = str(self.orch_server.call(self.orch.add_preset, "chat").id)
        migrate = subprocess.run(
            [console_script(), "migrate"],
            env=self._env(0),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert migrate.returncode == 0, migrate.stderr
        self.procs["api"] = self._spawn("api", self.api_port)
        self.start_bot()
        wait_for(lambda: self._ready("api", self.api_port), "api readyz")
        wait_for(lambda: self._ready("api", self.agent_port), "agent listener readyz")

    def start_bot(self) -> None:
        """Start the bot and wait until it holds the lock and finished its first sync."""
        self.procs["bot"] = self._spawn("bot", self.bot_port)
        wait_for(lambda: self._ready("bot", self.bot_port), "bot readyz")

    def stop_bot(self) -> int:
        """SIGTERM the bot and wait for it to exit; returns its exit code."""
        proc = self.procs.pop("bot")
        proc.send_signal(signal.SIGTERM)
        try:
            return proc.wait(20)
        finally:
            if proc.stderr:
                proc.stderr.close()

    def _ready(self, name: str, port: int) -> bool:
        code = self.procs[name].poll()
        assert code is None, f"{name} exited {code}: {self.procs[name].stderr.read()}"
        try:
            return self.http.get(f"http://127.0.0.1:{port}/readyz").status_code == 200
        except httpx.TransportError:
            return False

    def stop(self) -> None:
        self.http.close()
        for proc in self.procs.values():
            proc.terminate()
        for proc in self.procs.values():
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stderr:
                proc.stderr.close()
        self.hs_server.stop()
        self.orch_server.stop()
        with (
            contextlib.suppress(psycopg.Error),
            psycopg.connect(self.admin_url, autocommit=True) as conn,
        ):
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(self.db_name)
                )
            )

    # Matrix, through the fake homeserver's helpers ------------------------------------------

    def portal(
        self,
        room_type: str,
        members: dict[str, str],
        *,
        name: str | None = None,
        encrypted: bool = False,
    ) -> str:
        """A bridged portal room of `room_type` with these puppets (user id to display name),
        created by the bridge bot, which invites the bot; returns the room id once the
        connector lists it joined."""
        room_id = f"!{uuid.uuid4().hex[:12]}:{SERVER}"
        state = [bridge_state(room_type, name)]
        everyone = {BRIDGE_BOT: "Meta bridge bot", **members}
        self.hs_server.call(self.hs.add_room, room_id, state, everyone)
        if encrypted:
            self.hs_server.call(self.hs.encrypt, room_id)
        self.hs_server.call(self.hs.invite, room_id, BRIDGE_BOT)
        wait_for(
            lambda: (room := self.room(room_id)) and room["membership"] == "join" and room["kind"],
            f"room {room_id} to be joined and classified",
        )
        return room_id

    def post(self, room_id: str, sender: str, content: dict) -> str:
        return self.hs_server.call(self.hs.post, room_id, sender, content)

    def sent(self, room_id: str) -> list[dict]:
        return self.hs_server.call(self.hs.sent, room_id)

    # The orchestrator, through the fake orchestrator's helpers ------------------------------

    def sessions(self) -> list[dict[str, Any]]:
        return self.orch_server.call(lambda: [dict(s) for s in self.orch.sessions.values()])

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.orch_server.call(
            lambda: [dict(m) for m in self.orch.messages.values() if m["session_id"] == session_id]
        )

    def complete_turn(self, message_id: int, final_text: str) -> None:
        self.orch_server.call(self.orch.complete_turn, message_id, final_text)

    def discard_session(self, session_id: str) -> None:
        self.orch_server.call(self.orch.discard_session, session_id)

    def remove_session(self, session_id: str) -> None:
        self.orch_server.call(self.orch.remove_session, session_id)

    def add_session(self, slug: str) -> dict[str, Any]:
        """A running session the connector did not create (a slug without the `mx-` prefix)."""

        def create() -> dict[str, Any]:
            session_id = str(uuid.uuid4())
            session = {
                "id": session_id,
                "slug": slug,
                "preset_id": self.preset_id,
                "preset_name": "chat",
                "desired": "active",
                "created_at": datetime.now(UTC).isoformat(),
            }
            self.orch.sessions[session_id] = session
            return dict(session)

        return self.orch_server.call(create)

    def wait_session(self, count: int) -> list[dict[str, Any]]:
        """Wait until the orchestrator holds `count` sessions; returns them oldest first."""
        return wait_for(
            lambda: (found := self.sessions()) and len(found) >= count and found,
            f"{count} sessions",
        )

    def wait_prompt(self, session_id: str, count: int) -> dict[str, Any]:
        """Wait until the session holds `count` messages; returns the newest."""
        found = wait_for(
            lambda: (msgs := self.session_messages(session_id)) and len(msgs) >= count and msgs,
            f"{count} messages in session {session_id}",
        )
        return found[-1]

    # The connector, through /matrix/api/v1 --------------------------------------------------

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.api_port}{API}"

    def rooms(self) -> list[dict[str, Any]]:
        r = self.http.get(f"{self.api}/rooms")
        assert r.status_code == 200, r.text
        return r.json()

    def room(self, room_id: str) -> dict[str, Any] | None:
        r = self.http.get(f"{self.api}/rooms/{quote(room_id, safe='')}")
        if r.status_code == 404:
            return None
        assert r.status_code == 200, r.text
        return r.json()

    def messages(self, room_id: str) -> list[dict[str, Any]]:
        """The room's merged feed, oldest first."""
        r = self.http.get(f"{self.api}/rooms/{quote(room_id, safe='')}/messages?limit=200")
        assert r.status_code == 200, r.text
        return list(reversed(r.json()))

    def inbound(self, room_id: str) -> list[dict[str, Any]]:
        return [m for m in self.messages(room_id) if m["direction"] == "in"]

    def wait_inbound(
        self, room_id: str, count: int, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Wait until the room lists `count` inbound rows, all with `status` when given."""

        def probe() -> list[dict[str, Any]] | None:
            rows = self.inbound(room_id)
            if len(rows) < count:
                return None
            if status is not None and any(r["status"] != status for r in rows[:count]):
                return None
            return rows

        try:
            return wait_for(probe, f"{count} inbound rows of {room_id} ({status or 'any'})")
        except pytest.fail.Exception:
            pytest.fail(f"inbound rows of {room_id}: {self.inbound(room_id)}")

    def add_rule(self, list_: str, kind: str, value: str) -> dict[str, Any]:
        r = self.http.post(f"{self.api}/rules", json={"list": list_, "kind": kind, "value": value})
        assert r.status_code == 201, r.text
        return r.json()

    def set_default_preset(self) -> None:
        r = self.http.put(f"{self.api}/settings", json={"default_preset_id": self.preset_id})
        assert r.status_code == 200, r.text
        assert r.json() == {"default_preset_id": self.preset_id}

    def flag_room(self, room_id: str) -> None:
        """Let agents start a chat in the room (`agents_may_initiate`)."""
        r = self.http.put(
            f"{self.api}/rooms/{quote(room_id, safe='')}/settings",
            json={"agents_may_initiate": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["agents_may_initiate"] is True

    def allow_room(self, room_id: str) -> None:
        self.add_rule("allow", "room", room_id)
        self.set_default_preset()

    # The agent, through the agent listener's /chat/v1 --------------------------------------

    def chat(
        self,
        method: str,
        path: str,
        session: dict[str, Any],
        *,
        json: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        """A `/chat/v1` call for `session` with the test bearer, the session id and its slug."""
        return self.http.request(
            method,
            f"http://127.0.0.1:{self.agent_port}{CHAT}{path}",
            json=json,
            headers={
                "Authorization": f"Bearer {token or self.agent_token}",
                "X-Orch-Session": session["id"],
                "X-Orch-Slug": session["slug"],
            },
        )

    def send(
        self,
        session: dict[str, Any],
        body: str,
        *,
        room: str | None = None,
        reply_to: str | None = None,
    ) -> httpx.Response:
        payload = {"text": body, "room": room, "reply_to": reply_to}
        return self.chat(
            "POST", "/messages", session, json={k: v for k, v in payload.items() if v is not None}
        )

    def react(self, session: dict[str, Any], to: str, key: str) -> httpx.Response:
        return self.chat("POST", "/reactions", session, json={"to": to, "key": key})

    def wait_send(self, session: dict[str, Any], send_id: str, status: str) -> dict[str, Any]:
        """Wait until the session's send has this status; returns its status view."""

        def probe() -> dict[str, Any] | None:
            r = self.chat("GET", f"/messages/{send_id}", session)
            assert r.status_code == 200, r.text
            view = r.json()
            assert view["status"] not in ("failed", "refused") or view["status"] == status, view
            return view if view["status"] == status else None

        return wait_for(probe, f"send {send_id} to be {status}")


@pytest.fixture
def stack(pg_url: str) -> Iterator[Stack]:
    """A private stack per test: its own database, fakes, api and bot."""
    each = Stack(pg_url)
    try:
        each.start()
        yield each
    finally:
        each.stop()
