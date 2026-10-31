from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager, closing
from unittest.mock import Mock

import pytest

pytest.importorskip("mcp")

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import mcp_http
from mcp_http import create_http_app, serve_http, validate_public_url
from mcp_server import ConfigurationError
from storage import Storage


TOKEN = "synthetic-test-bearer-token-1234567890"
BASE_URL = "https://inventory.example.test"
TOOL_NAMES = {
    "list_households", "create_household", "list_stores", "create_store", "list_items", "add_item",
    "change_item_quantity", "set_item_quantity", "set_item_minimum", "archive_item", "restore_item", "shopping_list",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def inventory(tmp_path):
    storage = Storage(tmp_path / "http-inventory.sqlite3")
    storage.record_user(101, None, None)
    home = storage.create_household(101, "Synthetic home")
    store = storage.list_stores(home.id)[0]
    storage.record_user(202, None, None)
    other_home = storage.create_household(202, "Other home")
    other_store = storage.list_stores(other_home.id)[0]
    other_item = storage.add_or_update_item(other_store.id, 202, "Other stock", 3, "кг", 1)
    return storage, home, store, other_home, other_store, other_item


@pytest.fixture
def app(inventory):
    return create_http_app(
        inventory[0], 101, TOKEN,
        allowed_hosts=["inventory.example.test"], allowed_origins=[BASE_URL],
    )


@asynccontextmanager
async def http_client(app, *, headers=None, event_hooks=None):
    # ASGITransport does not run lifespan itself. Enter the SDK's real lifespan
    # explicitly so successful MCP calls exercise session-manager startup too.
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE_URL,
            headers=headers, event_hooks=event_hooks, timeout=10,
        ) as client:
            yield client


async def call(client, tool_name, **arguments):
    result = await client.call_tool(tool_name, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize("mode,negotiation_method", [("auto", "server/discover"), ("legacy", "initialize")])
async def test_real_http_sdk_negotiation_tools_and_inventory(app, inventory, mode, negotiation_method):
    storage, home, store, *_ = inventory
    requests, responses = [], []

    async def capture_request(request):
        if request.method == "POST":
            requests.append(json.loads(request.content)["method"])

    async def capture_response(response):
        responses.append(response)

    async with http_client(
        app, headers={"Authorization": f"Bearer {TOKEN}"},
        event_hooks={"request": [capture_request], "response": [capture_response]},
    ) as http:
        async with Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http), mode=mode) as connected:
            tools = (await connected.list_tools()).tools
            assert {tool.name for tool in tools} == TOOL_NAMES
            for tool in tools:
                assert tool.output_schema
                assert tool.input_schema["additionalProperties"] is False
                assert not {"user_id", "chat_id", "database_path"} & tool.input_schema.get("properties", {}).keys()
                assert tool.annotations.open_world_hint is False
            assert await call(connected, "list_households") == {
                "households": [{"id": home.id, "name": "Synthetic home"}],
            }
            item = await call(connected, "add_item", store_id=store.id, name="Rice", quantity=2, unit="kg")
            converted = await call(connected, "change_item_quantity", item_id=item["id"], delta=-500, unit="g")
            assert (converted["quantity"], converted["unit"]) == (1.5, "кг")
    assert negotiation_method in requests
    assert all("mcp-session-id" not in response.headers for response in responses)
    assert storage.get_item(item["id"]).quantity == 1.5


@pytest.mark.anyio
async def test_http_tools_keep_household_isolation_and_fixed_identity(app, inventory):
    storage, home, store, other_home, other_store, other_item = inventory
    async with http_client(app, headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        async with Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as connected:
            for name, arguments in [
                ("list_stores", {"household_id": other_home.id}),
                ("shopping_list", {"household_id": other_home.id}),
                ("create_store", {"household_id": other_home.id, "name": "Blocked store"}),
                ("list_items", {"store_id": other_store.id}),
                ("add_item", {"store_id": other_store.id, "name": "Blocked item", "quantity": 1}),
                ("set_item_quantity", {"item_id": other_item.id, "quantity": 0}),
                ("archive_item", {"item_id": other_item.id}),
                ("list_items", {"store_id": store.id, "chat_id": 202}),
            ]:
                result = await connected.call_tool(name, arguments)
                assert result.is_error, name
            with closing(sqlite3.connect(storage.path)) as connection:
                connection.execute(
                    "DELETE FROM household_members WHERE household_id = ? AND chat_id = ?", (home.id, 101),
                )
                connection.commit()
            denied_after_revocation = await connected.call_tool("list_items", {"store_id": store.id})
            assert denied_after_revocation.is_error
    assert storage.get_item(other_item.id).quantity == 3
    assert storage.get_item(other_item.id).active


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
async def test_authentication_covers_every_http_method_and_unknown_path(app, inventory, monkeypatch, caplog, method):
    storage = inventory[0]
    access = Mock(side_effect=AssertionError("Unauthenticated request reached inventory"))
    monkeypatch.setattr(storage, "get_user", access)
    header_cases = [
        [],
        [("Authorization", "Bearer wrong-token")],
        [("Authorization", f"Basic {TOKEN}")],
        [("Authorization", "Bearer")],
        [("Authorization", f"Bearer  {TOKEN}")],
        [("Authorization", f"Bearer {TOKEN} extra")],
        [("Authorization", f"Bearer {TOKEN},")],
        [("Authorization", f"Bearer {TOKEN}"), ("authorization", f"Bearer {TOKEN}")],
    ]
    async with http_client(app) as http:
        for path in ("/mcp", "/mcp/", "/", "/unknown", "/.well-known/oauth-protected-resource"):
            for headers in header_cases:
                response = await http.request(method, path, headers=headers, content=b"not JSON")
                assert response.status_code == 401, (method, path, headers)
                assert response.headers["www-authenticate"] == "Bearer"
                assert TOKEN not in response.text
                assert "access-control-allow-origin" not in response.headers
        if method not in {"GET", "HEAD"}:
            assert (await http.request(method, "/healthz")).status_code == 401
        # Cookies and query parameters cannot substitute for the authorization header.
        response = await http.request(
            method, f"/mcp?access_token={TOKEN}", headers={"Cookie": f"token={TOKEN}"},
        )
        assert response.status_code == 401
    access.assert_not_called()
    assert TOKEN not in caplog.text


@pytest.mark.anyio
async def test_health_probe_is_static_and_does_not_read_inventory(app, inventory, monkeypatch):
    access = Mock(side_effect=AssertionError("Health probe reached inventory"))
    monkeypatch.setattr(inventory[0], "get_user", access)
    async with http_client(app) as http:
        get_response = await http.get("/healthz?ignored=anything")
        assert get_response.status_code == 200
        assert get_response.json() == {"status": "ok"}
        assert get_response.headers["cache-control"] == "no-store"
        head_response = await http.head("/healthz")
        assert head_response.status_code == 200 and not head_response.content
        assert (await http.get("/healthz/")).status_code == 401
    access.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("headers,status", [
    ({"Host": "attacker.example.test"}, 421),
    ({"Origin": "https://attacker.example.test"}, 403),
    ({"Origin": "null"}, 403),
    ({"Origin": ""}, 403),
    ([("Host", "inventory.example.test"), ("Host", "attacker.example.test")], 421),
    ([("Origin", BASE_URL), ("Origin", "https://attacker.example.test")], 403),
])
async def test_valid_token_still_requires_trusted_host_and_origin(app, inventory, monkeypatch, headers, status):
    access = Mock(side_effect=AssertionError("Untrusted origin reached inventory"))
    monkeypatch.setattr(inventory[0], "get_user", access)
    async with http_client(app, headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        response = await http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == status
        assert TOKEN not in response.text
        assert (await http.get("/unknown", headers=headers)).status_code == status
    access.assert_not_called()


@pytest.mark.anyio
async def test_explicit_trusted_origin_works_without_cors_or_oauth_discovery(app):
    async with http_client(app, headers={"Authorization": f"bEaReR {TOKEN}", "Origin": BASE_URL}) as http:
        async with Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as connected:
            assert len((await connected.list_tools()).tools) == 12
        discovery = await http.get("/.well-known/oauth-protected-resource")
        assert discovery.status_code == 404
        assert "access-control-allow-origin" not in discovery.headers


@pytest.mark.parametrize("hosts", [[], "*", ["*"], ["*.example.test"], ["example.test:*"], ["https://example.test"],
                                         ["user@example.test"], ["example.test/path"], ["example.test:0"], ["example.test:65536"], [None]])
def test_http_requires_explicit_usable_host_allowlist(inventory, hosts):
    with pytest.raises(ConfigurationError, match="host"):
        create_http_app(inventory[0], 101, TOKEN, allowed_hosts=hosts, allowed_origins=[])


@pytest.mark.parametrize("token", ["", "white space", "secret\n", "токен", "token:colon", None])
def test_http_rejects_malformed_configured_token_without_echo(inventory, token):
    with pytest.raises(ConfigurationError, match="MCP_AUTH_TOKEN") as error:
        create_http_app(inventory[0], 101, token, allowed_hosts=["localhost:8000"], allowed_origins=[])
    if token:
        assert token not in str(error.value)


@pytest.mark.parametrize("value", [
    "http://inventory.example.test", "https://user:password@example.test", "https://example.test/mcp",
    "https://example.test?token=synthetic", "https://example.test#fragment", "https://example.test?",
    "https://example.test#", "ftp://example.test", "//example.test", "https://*.example.test",
    "https://example.test:0", "https://example.test:65536", "https://example.test:", "https://example.test/../",
    "https://example.test\\@localhost", "https://example.test\n", "", 123,
])
def test_public_url_rejects_unsafe_or_non_origin_configuration(value):
    with pytest.raises(ConfigurationError):
        validate_public_url(value)


@pytest.mark.parametrize("value,expected", [
    (None, None), ("https://inventory.example.test/", BASE_URL),
    ("https://inventory.example.test:8443", BASE_URL + ":8443"),
    ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
    ("http://localhost:8000", "http://localhost:8000"), ("http://[::1]:8000", "http://[::1]:8000"),
])
def test_public_url_accepts_https_and_local_http_origins(value, expected):
    assert validate_public_url(value) == expected


@pytest.mark.parametrize("public_url", [None, "https://inventory.example.test", "https://inventory.example.test:443/",
                                      "http://[::1]:8000", "https://[::1]:8443"])
def test_runner_uses_explicit_allowlists_sdk_lifespan_and_disables_access_log(inventory, monkeypatch, public_url):
    create = Mock(wraps=create_http_app)
    run = Mock()
    monkeypatch.setattr(mcp_http, "create_http_app", create)
    monkeypatch.setattr(mcp_http.uvicorn, "run", run)
    profile_id = "prf_" + "h" * 43
    serve_http(inventory[0], 101, TOKEN, host="0.0.0.0", port=8000, public_url=public_url, profile_id=profile_id)
    assert create.call_args.kwargs["profile_id"] == profile_id
    hosts = create.call_args.kwargs["allowed_hosts"]
    origins = create.call_args.kwargs["allowed_origins"]
    assert {"127.0.0.1:8000", "localhost:8000", "[::1]:8000"} <= set(hosts)
    assert "0.0.0.0:8000" not in hosts
    assert "*" not in hosts and not any("*" in value for value in hosts + origins)
    if public_url and "inventory.example.test" in public_url:
        assert "inventory.example.test" in hosts
        assert validate_public_url(public_url) in origins
    run.assert_called_once()
    assert isinstance(run.call_args.args[0], mcp_http.Starlette)
    assert run.call_args.kwargs == {"host": "0.0.0.0", "port": 8000, "access_log": False, "log_level": "info"}


@pytest.mark.parametrize("port", [0, -1, 65536, True, "8000"])
def test_runner_rejects_invalid_port_before_binding(inventory, monkeypatch, port):
    run = Mock()
    monkeypatch.setattr(mcp_http.uvicorn, "run", run)
    with pytest.raises(ConfigurationError, match="PORT"):
        serve_http(inventory[0], 101, TOKEN, host="127.0.0.1", port=port, public_url=None)
    run.assert_not_called()
