"""The mautrix-meta provisioning client."""

import json
import traceback

import httpx
import pytest

from agents_matrix_connector.bridge import (
    BridgeError,
    BridgeUnavailable,
    BridgeUser,
    ProvisioningClient,
)

BASE = "http://bridge:29328/_matrix/provision"
TOKEN = "syt_aGVybWVz_bridge_secret_token"
HERMES = "@hermes:szymonrichert.pl"


class Bridge:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answers: list[httpx.Response] = []

    def answer(self, status: int, body) -> None:
        self.answers.append(httpx.Response(status, json=body))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answers.pop(0)


@pytest.fixture
def bridge():
    return Bridge()


@pytest.fixture
async def client(bridge):
    c = ProvisioningClient(BASE, TOKEN, HERMES, transport=httpx.MockTransport(bridge.handler))
    yield c
    await c.aclose()


async def test_whoami_passes_user_id_and_bearer(bridge, client):
    bridge.answer(200, {"management_room": "!mgmt:x", "logins": []})
    assert (await client.whoami())["management_room"] == "!mgmt:x"
    [request] = bridge.requests
    assert request.method == "GET"
    assert request.url.path == "/_matrix/provision/v3/whoami"
    assert request.url.params["user_id"] == HERMES
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_search_users_parses_results(bridge, client):
    bridge.answer(
        200,
        {
            "results": [
                {
                    "id": "100",
                    "name": "Jane Doe",
                    "mxid": "@facebook_100:szymonrichert.pl",
                    "dm_room_mxid": "!dm:x",
                },
                {"id": "200", "name": "Jane D"},
            ]
        },
    )
    found = await client.search_users("Jane")
    assert found == [
        BridgeUser("100", "Jane Doe", "@facebook_100:szymonrichert.pl", "!dm:x"),
        BridgeUser("200", "Jane D", None, None),
    ]
    [request] = bridge.requests
    assert request.method == "POST"
    assert request.url.path == "/_matrix/provision/v3/search_users"
    assert request.url.params["user_id"] == HERMES
    assert json.loads(request.content) == {"query": "Jane"}
    bridge.answer(200, {"results": None})
    assert await client.search_users("x") == []


async def test_create_dm_returns_room(bridge, client):
    bridge.answer(
        201, {"id": "100", "name": "Jane", "mxid": "@facebook_100:x", "dm_room_mxid": "!new:x"}
    )
    user = await client.create_dm("100")
    assert user == BridgeUser("100", "Jane", "@facebook_100:x", "!new:x")
    [request] = bridge.requests
    assert request.method == "POST"
    assert request.url.path == "/_matrix/provision/v3/create_dm/100"
    assert request.url.params["user_id"] == HERMES


async def test_create_dm_existing_200(bridge, client):
    bridge.answer(200, {"id": "100", "dm_room_mxid": "!old:x"})
    assert (await client.create_dm("100")).dm_room_mxid == "!old:x"


async def test_not_found_raises_bridge_error(bridge, client):
    bridge.answer(404, {"errcode": "M_NOT_FOUND", "error": "no such user"})
    with pytest.raises(BridgeError) as exc:
        await client.create_dm("1")
    assert not isinstance(exc.value, BridgeUnavailable)
    assert (exc.value.status, exc.value.errcode) == (404, "M_NOT_FOUND")


async def test_5xx_unavailable(bridge, client):
    bridge.answer(502, {"errcode": "M_UNKNOWN", "error": "upstream"})
    with pytest.raises(BridgeUnavailable) as exc:
        await client.search_users("x")
    assert exc.value.status == 502


async def test_transport_error_unavailable():
    def boom(request):
        raise httpx.ConnectError(f"refused {TOKEN}")

    c = ProvisioningClient(BASE, TOKEN, HERMES, transport=httpx.MockTransport(boom))
    with pytest.raises(BridgeUnavailable) as exc:
        await c.whoami()
    assert exc.value.status is None
    assert TOKEN not in str(exc.value)
    await c.aclose()


async def test_token_not_in_repr_or_errors(bridge, client):
    assert TOKEN not in repr(client) and TOKEN not in str(client)
    bridge.answer(400, {"errcode": "M_BAD_JSON", "error": f"bad token {TOKEN}"})
    with pytest.raises(BridgeError) as exc:
        await client.search_users("x")
    text = str(exc.value) + repr(exc.value) + "".join(traceback.format_exception(exc.value))
    assert TOKEN not in text
