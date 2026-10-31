"""Acceptance checks for OAuth and existing Bearer clients in the same app."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

pytest.importorskip("mcp")

from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_http import create_http_app
from mcp_oauth import OAuthProvider
from test_http import TOKEN, TOOL_NAMES, anyio_backend, call, http_client, inventory
from test_oauth import ISSUER, RESOURCE, register, tokens


@pytest.fixture
def oauth(tmp_path):
    return OAuthProvider(tmp_path / "oauth.sqlite3", ISSUER)


@pytest.fixture
def combined_app(inventory, oauth):
    return create_http_app(
        inventory[0], 101, TOKEN, allowed_hosts=["inventory.example.test"],
        allowed_origins=[ISSUER], oauth=oauth,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("client_method", ["none", "client_secret_basic"])
async def test_complete_public_oauth_flow_opens_scoped_mcp_and_keeps_static_bearer(combined_app, inventory, oauth, client_method):
    _, home, _, other_home, *_ = inventory
    requests = set()

    async def capture(request):
        requests.add((request.method, request.url.path))

    async with http_client(combined_app, event_hooks={"request": [capture]}) as http:
        anonymous = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"] == f'Bearer resource_metadata="{oauth.resource_metadata_url}"'
        resource = await http.get(oauth.resource_metadata_url)
        assert resource.status_code == 200
        assert resource.json()["resource"] == RESOURCE
        assert resource.json()["authorization_servers"] == [ISSUER]
        metadata = await http.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["registration_endpoint"] == ISSUER + "/register"
        registration = await register(http, method=client_method)
        issued, _, _ = await tokens(http, oauth, registration)
        http.headers["Authorization"] = "Bearer " + issued["access_token"]
        async with Client(streamable_http_client(RESOURCE, http_client=http)) as connected:
            assert {tool.name for tool in (await connected.list_tools()).tools} == TOOL_NAMES
            assert len(TOOL_NAMES) == 12
            assert await call(connected, "list_households") == {
                "households": [{"id": home.id, "name": "Synthetic home"}],
            }
            foreign = await connected.call_tool("list_stores", {"household_id": other_home.id})
            assert foreign.is_error
        http.headers["Authorization"] = "Bearer " + TOKEN
        async with Client(streamable_http_client(RESOURCE, http_client=http), mode="legacy") as connected:
            assert {tool.name for tool in (await connected.list_tools()).tools} == TOOL_NAMES
            assert await call(connected, "list_households") == {
                "households": [{"id": home.id, "name": "Synthetic home"}],
            }
    assert {
        ("GET", "/.well-known/oauth-protected-resource/mcp"),
        ("GET", "/.well-known/oauth-authorization-server"), ("POST", "/register"),
        ("GET", "/authorize"), ("GET", "/oauth/consent"), ("POST", "/oauth/consent"),
        ("POST", "/token"), ("POST", "/mcp"),
    } <= requests


@pytest.mark.anyio
async def test_refresh_preserves_existing_access_and_revocation_invalidates_family(combined_app, oauth):
    async with http_client(combined_app) as http:
        registration = await register(http)
        issued, *_ = await tokens(http, oauth, registration)
        rotated = await http.post("/token", data={
            "grant_type": "refresh_token", "client_id": registration["client_id"],
            "refresh_token": issued["refresh_token"], "resource": RESOURCE,
        })
        assert rotated.status_code == 200
        renewed = rotated.json()
        for access in (issued["access_token"], renewed["access_token"]):
            http.headers["Authorization"] = "Bearer " + access
            async with Client(streamable_http_client(RESOURCE, http_client=http)) as connected:
                assert {tool.name for tool in (await connected.list_tools()).tools} == TOOL_NAMES
        del http.headers["Authorization"]
        revoked = await http.post("/revoke", data={
            "client_id": registration["client_id"], "token": renewed["refresh_token"],
            "token_type_hint": "refresh_token",
        })
        assert revoked.status_code == 200
        for access in (issued["access_token"], renewed["access_token"]):
            denied = await http.post("/mcp", headers={"Authorization": "Bearer " + access}, json={})
            assert denied.status_code == 401
        http.headers["Authorization"] = "Bearer " + TOKEN
        async with Client(streamable_http_client(RESOURCE, http_client=http)) as connected:
            assert len((await connected.list_tools()).tools) == 12


@pytest.mark.anyio
async def test_old_access_opens_mcp_while_refresh_response_is_in_transit(combined_app, oauth, monkeypatch):
    committed, release = asyncio.Event(), asyncio.Event()
    exchange = oauth.exchange_refresh_token

    async def delay_response(*args):
        result = await exchange(*args)
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(oauth, "exchange_refresh_token", delay_response)
    async with http_client(combined_app) as http:
        registration = await register(http)
        issued, *_ = await tokens(http, oauth, registration)
        refreshing = asyncio.create_task(http.post("/token", data={
            "grant_type": "refresh_token", "client_id": registration["client_id"],
            "refresh_token": issued["refresh_token"], "resource": RESOURCE,
        }))
        try:
            await asyncio.wait_for(committed.wait(), timeout=5)
            assert not refreshing.done()
            http.headers["Authorization"] = "Bearer " + issued["access_token"]
            async with Client(streamable_http_client(RESOURCE, http_client=http)) as connected:
                assert {tool.name for tool in (await connected.list_tools()).tools} == TOOL_NAMES
        finally:
            http.headers.pop("Authorization", None)
            release.set()
            rotated = await asyncio.wait_for(refreshing, timeout=5)
        assert rotated.status_code == 200
        assert await oauth.verify_token(rotated.json()["access_token"])


@pytest.mark.anyio
async def test_public_oauth_routes_still_reject_untrusted_hosts(combined_app, inventory, monkeypatch):
    access = Mock(side_effect=AssertionError("Public OAuth request reached inventory"))
    monkeypatch.setattr(inventory[0], "get_user", access)
    async with http_client(combined_app) as http:
        for method, path in (
            ("GET", "/.well-known/oauth-protected-resource/mcp"),
            ("GET", "/.well-known/oauth-authorization-server"), ("POST", "/register"),
            ("GET", "/authorize"), ("POST", "/token"), ("POST", "/revoke"),
            ("GET", "/oauth/consent"),
        ):
            response = await http.request(method, path, headers={"Host": "untrusted.example.test"})
            assert response.status_code == 421, (method, path)
        duplicated = await http.get("/.well-known/oauth-authorization-server", headers=[
            ("Host", "inventory.example.test"), ("Host", "untrusted.example.test"),
        ])
        assert duplicated.status_code == 421
    access.assert_not_called()


@pytest.mark.anyio
async def test_only_exact_oauth_paths_and_supported_methods_are_public(combined_app):
    async with http_client(combined_app) as http:
        for method, path in (
            ("GET", "/register"), ("PUT", "/token"), ("DELETE", "/authorize"),
            ("POST", "/.well-known/oauth-authorization-server"), ("GET", "/register/"),
            ("GET", "/oauth/consent/"), ("POST", "/token/"),
            ("GET", "/.well-known/oauth-protected-resource/mcp/"),
        ):
            response = await http.request(method, path)
            assert response.status_code == 401, (method, path)
            assert "resource_metadata=" in response.headers["www-authenticate"]


@pytest.mark.anyio
async def test_malformed_or_duplicate_authorization_cannot_open_mcp(combined_app, inventory, oauth, monkeypatch):
    async with http_client(combined_app) as http:
        registration = await register(http)
        issued, *_ = await tokens(http, oauth, registration)
        bearer = "Bearer " + issued["access_token"]
        access = Mock(side_effect=AssertionError("Invalid authorization reached inventory"))
        monkeypatch.setattr(inventory[0], "get_user", access)
        for headers in (
            {"Authorization": "Basic " + issued["access_token"]},
            {"Authorization": "Bearer  " + issued["access_token"]},
            {"Authorization": bearer + ","}, {"Authorization": " " + bearer},
            [("Authorization", bearer), ("Authorization", bearer)],
            [("Authorization", bearer), ("Authorization", "Bearer " + TOKEN)],
        ):
            response = await http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 401
            assert issued["access_token"] not in response.text
        access.assert_not_called()
