from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytest.importorskip("mcp")

import httpx2
from mcp.server.auth.provider import TokenError
from starlette.applications import Starlette

import mcp_oauth
from mcp_oauth import OAuthProvider, create_oauth_routes
from mcp_server import ConfigurationError


ISSUER = "https://inventory.example.test"
RESOURCE = ISSUER + "/mcp"
CALLBACK = "https://client.example.test/oauth/callback"
VERIFIER = "synthetic-test-verifier-" + "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def provider(tmp_path):
    return OAuthProvider(tmp_path / "oauth.sqlite3", ISSUER)


@pytest.fixture
async def client(provider):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=Starlette(routes=create_oauth_routes(provider))), base_url=ISSUER,
    ) as client:
        yield client


async def register(client, *, method="none", **updates):
    response = await client.post("/register", json={
        "redirect_uris": [CALLBACK], "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "client_name": "Synthetic client", **updates,
    })
    assert response.status_code == 201, response.text
    return response.json()


async def begin(client, registration, **updates):
    response = await client.get("/authorize", params={
        "client_id": registration["client_id"], "redirect_uri": CALLBACK, "response_type": "code",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": RESOURCE,
        "scope": "inventory", "state": "synthetic-state", **updates,
    })
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(ISSUER + "/oauth/consent?")
    page = await client.get(location)
    assert page.status_code == 200, page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    nonce = parse_qs(urlsplit(location).query)["request"][0]
    return {"request": nonce, "csrf": csrf}, page


async def approve(client, provider, registration, *, owner_code=None):
    fields, page = await begin(client, registration)
    response = await client.post("/oauth/consent", data={
        **fields, "code": owner_code if owner_code is not None else provider.issue_owner_code(), "decision": "allow",
    }, headers={"Origin": ISSUER})
    assert response.status_code == 302, response.text
    values = parse_qs(urlsplit(response.headers["location"]).query)
    assert values["state"] == ["synthetic-state"]
    assert values["iss"] == [ISSUER]
    return values["code"][0], page


async def exchange(client, registration, code, **updates):
    data = {
        "grant_type": "authorization_code", "client_id": registration["client_id"], "code": code,
        "redirect_uri": CALLBACK, "code_verifier": VERIFIER, "resource": RESOURCE, **updates,
    }
    headers = {}
    if registration["token_endpoint_auth_method"] == "client_secret_basic":
        data.pop("client_id", None)
        basic = base64.b64encode((registration["client_id"] + ":" + registration["client_secret"]).encode()).decode()
        headers["Authorization"] = "Basic " + basic
    elif registration["token_endpoint_auth_method"] == "client_secret_post":
        data["client_secret"] = registration["client_secret"]
    return await client.post("/token", data=data, headers=headers)


async def tokens(client, provider, registration):
    code, page = await approve(client, provider, registration)
    response = await exchange(client, registration, code)
    assert response.status_code == 200, response.text
    return response.json(), code, page


@pytest.mark.anyio
async def test_sdk_discovery_dcr_consent_pkce_and_restart(client, provider):
    metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == ISSUER
    assert metadata["registration_endpoint"] == ISSUER + "/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["none", "client_secret_post", "client_secret_basic"]
    assert metadata["scopes_supported"] == ["inventory"]
    assert metadata["client_id_metadata_document_supported"] is False
    resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["resource"] == RESOURCE
    assert resource["authorization_servers"] == [ISSUER]
    registration = await register(client)
    result, code, page = await tokens(client, provider, registration)
    assert result["scope"] == "inventory" and result["expires_in"] == mcp_oauth.ACCESS_TTL
    assert len(re.findall(r"<li>", page.text)) == 12
    assert "Разрешить доступ" in page.text and "Отказать" in page.text
    assert "HttpOnly" in page.headers["set-cookie"] and "Secure" in page.headers["set-cookie"]
    assert page.headers["referrer-policy"] == "no-referrer"
    restarted = OAuthProvider(provider.database_path, ISSUER)
    assert await restarted.verify_token(result["access_token"])
    loaded = await restarted.load_access_token(result["access_token"])
    assert loaded.subject == "inventory-owner" and loaded.resource == RESOURCE
    assert result["access_token"] not in repr(loaded)
    assert not await restarted.verify_token(result["refresh_token"])
    assert (await exchange(client, registration, code)).status_code == 400


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
async def test_public_and_confidential_clients_store_only_credential_hashes(client, provider, method):
    registration = await register(client, method=method)
    owner_code = provider.issue_owner_code()
    code, _ = await approve(client, provider, registration, owner_code=owner_code)
    response = await exchange(client, registration, code)
    assert response.status_code == 200, response.text
    result = response.json()
    assert await provider.verify_token(result["access_token"])
    raw_database = provider.database_path.read_bytes()
    for secret in (owner_code, code, result["access_token"], result["refresh_token"], registration.get("client_secret")):
        if secret:
            assert secret.encode() not in raw_database
    assert (await provider.get_client(registration["client_id"])).client_secret is None


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
async def test_confidential_clients_can_refresh_and_revoke_with_hashed_secrets(client, provider, method):
    registration = await register(client, method=method)
    issued, *_ = await tokens(client, provider, registration)
    authentication = {"client_id": registration["client_id"], "client_secret": registration["client_secret"]}
    headers = {}
    if method == "client_secret_basic":
        authentication = {}
        basic = base64.b64encode((registration["client_id"] + ":" + registration["client_secret"]).encode()).decode()
        headers["Authorization"] = "Basic " + basic
    response = await client.post("/token", data={**authentication, "grant_type": "refresh_token",
                                "refresh_token": issued["refresh_token"], "resource": RESOURCE}, headers=headers)
    assert response.status_code == 200, response.text
    renewed = response.json()
    assert await provider.verify_token(renewed["access_token"])
    revoked = await client.post("/revoke", data={**authentication, "token": renewed["refresh_token"]}, headers=headers)
    assert revoked.status_code == 200, revoked.text
    assert not await provider.verify_token(renewed["access_token"])


@pytest.mark.anyio
async def test_authorization_code_only_registration_uses_sdk_without_refresh(client, provider):
    registration = await register(client, grant_types=["authorization_code"])
    result, *_ = await tokens(client, provider, registration)
    assert not result.get("refresh_token")
    assert await provider.verify_token(result["access_token"])


@pytest.mark.anyio
async def test_pkce_client_redirect_resource_and_scope_cannot_be_changed(client, provider):
    registration, other = await register(client), await register(client)
    code, _ = await approve(client, provider, registration)
    for updates in (
        {"code_verifier": "x" * 64}, {"redirect_uri": CALLBACK + "/different"},
        {"client_id": other["client_id"]}, {"resource": "https://other.example.test/mcp"},
        {"code_verifier": "short"},
    ):
        response = await exchange(client, registration, code, **updates)
        assert response.status_code == 400, updates
        assert code not in response.text
    result = await exchange(client, registration, code)
    assert result.status_code == 200
    issued = result.json()
    for updates in ({"scope": "admin"}, {"resource": "https://other.example.test/mcp"}):
        response = await client.post("/token", data={
            "grant_type": "refresh_token", "client_id": registration["client_id"],
            "refresh_token": issued["refresh_token"], "resource": RESOURCE, **updates,
        })
        assert response.status_code == 400
        assert issued["refresh_token"] not in response.text
    assert await provider.verify_token(issued["access_token"])


@pytest.mark.anyio
async def test_missing_token_resource_inherits_grant_but_wrong_authorization_resource_is_denied(client, provider):
    registration = await register(client)
    for value in (None, "", "https://other.example.test/mcp"):
        parameters = {
            "client_id": registration["client_id"], "redirect_uri": CALLBACK, "response_type": "code",
            "code_challenge": CHALLENGE, "state": "synthetic-state",
        }
        if value is not None:
            parameters["resource"] = value
        response = await client.get("/authorize", params=parameters)
        assert response.status_code == 302
        assert "error" in parse_qs(urlsplit(response.headers["location"]).query)
    code, _ = await approve(client, provider, registration)
    response = await client.post("/token", data={
        "grant_type": "authorization_code", "client_id": registration["client_id"], "code": code,
        "redirect_uri": CALLBACK, "code_verifier": VERIFIER,
    })
    assert response.status_code == 200
    assert (await provider.load_access_token(response.json()["access_token"])).resource == RESOURCE


@pytest.mark.anyio
async def test_consent_requires_owner_code_cookie_origin_csrf_and_explicit_approval(client, provider):
    registration = await register(client)
    fields, _ = await begin(client, registration)
    owner_code = provider.issue_owner_code()
    data = {**fields, "code": owner_code, "decision": "allow"}
    cases = [({}, data), ({"Origin": "null"}, data), ({"Origin": "https://attacker.example.test"}, data),
             ({"Origin": ISSUER}, {**data, "csrf": "wrong"}),
             ([("Origin", ISSUER), ("Origin", ISSUER)], data)]
    for headers, altered in cases:
        response = await client.post("/oauth/consent", data=altered, headers=headers)
        assert response.status_code == 403
        assert owner_code not in response.text
    response = await client.post("/oauth/consent", data={**data, "decision": "missing"}, headers={"Origin": ISSUER})
    assert response.status_code == 400
    approved = await client.post("/oauth/consent", data=data, headers={"Origin": ISSUER})
    assert approved.status_code == 302
    reused, _ = await begin(client, registration)
    assert (await client.post("/oauth/consent", data={**reused, "code": owner_code, "decision": "allow"},
                              headers={"Origin": ISSUER})).status_code == 400


@pytest.mark.anyio
async def test_denial_consumes_pending_request_without_consuming_owner_code(client, provider):
    registration = await register(client)
    fields, _ = await begin(client, registration)
    owner = provider.issue_owner_code()
    response = await client.post("/oauth/consent", data={**fields, "decision": "deny"}, headers={"Origin": ISSUER})
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["access_denied"]
    assert (await client.get("/oauth/consent", params={"request": fields["request"]})).status_code == 400
    code, _ = await approve(client, provider, registration, owner_code=owner)
    assert (await exchange(client, registration, code)).status_code == 200


@pytest.mark.anyio
async def test_missing_cookie_and_cross_request_csrf_cannot_consume_owner_code(client, provider):
    registration = await register(client)
    fields, _ = await begin(client, registration)
    owner = provider.issue_owner_code()
    data = {**fields, "code": owner, "decision": "allow"}
    response = await client.post("/oauth/consent", data=data, headers={"Origin": ISSUER, "Cookie": ""})
    assert response.status_code == 403
    another, _ = await begin(client, registration)
    response = await client.post("/oauth/consent", data=data, headers={"Origin": ISSUER})
    assert response.status_code == 403
    approved = await client.post("/oauth/consent", data={**another, "code": owner, "decision": "allow"}, headers={"Origin": ISSUER})
    assert approved.status_code == 302


@pytest.mark.anyio
async def test_owner_code_expiry_replacement_and_attempt_budget_survive_restart(client, provider):
    registration = await register(client)
    expired = provider.issue_owner_code()
    with provider._db() as db:
        db.execute("UPDATE owner_codes SET expires_at = 0")
    fields, _ = await begin(client, registration)
    response = await client.post("/oauth/consent", data={**fields, "code": expired, "decision": "allow"}, headers={"Origin": ISSUER})
    assert response.status_code == 400
    replaced = provider.issue_owner_code()
    current = provider.issue_owner_code()
    with provider._db() as db:
        assert db.execute("SELECT COUNT(*) FROM owner_codes WHERE code_hash = ?", (hashlib.sha256(replaced.encode()).hexdigest(),)).fetchone()[0] == 0
    for _ in range(mcp_oauth.MAX_ATTEMPTS):
        fields, _ = await begin(client, registration)
        assert (await client.post("/oauth/consent", data={**fields, "code": "wrong", "decision": "allow"}, headers={"Origin": ISSUER})).status_code == 400
    restarted = OAuthProvider(provider.database_path, ISSUER)
    fields, _ = await begin(client, registration)
    assert (await client.post("/oauth/consent", data={**fields, "code": current, "decision": "allow"}, headers={"Origin": ISSUER})).status_code == 400
    replacement = restarted.issue_owner_code()
    assert (await approve(client, provider, registration, owner_code=replacement))[0]


@pytest.mark.anyio
async def test_refresh_rotation_replay_revokes_family_and_other_client_cannot_revoke(client, provider):
    registration, other = await register(client), await register(client)
    result, *_ = await tokens(client, provider, registration)
    response = await client.post("/revoke", data={"client_id": other["client_id"], "token": result["access_token"]})
    assert response.status_code == 200
    assert await provider.verify_token(result["access_token"])
    request = {"grant_type": "refresh_token", "client_id": registration["client_id"], "refresh_token": result["refresh_token"], "resource": RESOURCE}
    rotated = await client.post("/token", data=request)
    assert rotated.status_code == 200, rotated.text
    renewed = rotated.json()
    assert renewed["access_token"] != result["access_token"]
    assert renewed["refresh_token"] != result["refresh_token"]
    assert not await provider.verify_token(result["access_token"])
    assert await provider.verify_token(renewed["access_token"])
    assert (await client.post("/token", data=request)).status_code == 400
    assert not await provider.verify_token(renewed["access_token"])
    owner = await provider.get_client(registration["client_id"])
    assert await provider.load_refresh_token(owner, renewed["refresh_token"]) is None


@pytest.mark.anyio
async def test_atomic_code_consumption_and_concurrent_refresh_replay(client, provider):
    registration = await register(client)
    code, _ = await approve(client, provider, registration)
    owner = await provider.get_client(registration["client_id"])
    first = await provider.load_authorization_code(owner, code)
    second = await provider.load_authorization_code(owner, code)
    result = await provider.exchange_authorization_code(owner, first)
    assert result.access_token not in repr(result)
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(owner, second)
    refresh_one = await provider.load_refresh_token(owner, result.refresh_token)
    refresh_two = await provider.load_refresh_token(owner, result.refresh_token)
    assert result.refresh_token not in repr(refresh_one)
    renewed = await provider.exchange_refresh_token(owner, refresh_one, ["inventory"])
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(owner, refresh_two, ["inventory"])
    assert not await provider.verify_token(renewed.access_token)


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["access", "refresh", "code", "pending"])
async def test_expired_credentials_and_pending_requests_are_rejected(client, provider, target):
    registration = await register(client)
    if target == "pending":
        fields, _ = await begin(client, registration)
        with provider._db() as db:
            db.execute("UPDATE pending SET expires_at = 0")
        response = await client.post("/oauth/consent", data={**fields, "code": provider.issue_owner_code(), "decision": "allow"}, headers={"Origin": ISSUER})
        assert response.status_code == 403
        return
    code, _ = await approve(client, provider, registration)
    if target == "code":
        with provider._db() as db:
            db.execute("UPDATE codes SET expires_at = 0")
        assert (await exchange(client, registration, code)).status_code == 400
        return
    result = (await exchange(client, registration, code)).json()
    with provider._db() as db:
        db.execute(f"UPDATE grants SET {target}_expires = 0")
    if target == "access":
        assert not await provider.verify_token(result["access_token"])
    else:
        response = await client.post("/token", data={"grant_type": "refresh_token", "client_id": registration["client_id"], "refresh_token": result["refresh_token"]})
        assert response.status_code == 400


@pytest.mark.anyio
@pytest.mark.parametrize("hint", ["access_token", "refresh_token"])
async def test_revocation_invalidates_both_credentials_and_is_idempotent(client, provider, hint):
    registration = await register(client)
    result, *_ = await tokens(client, provider, registration)
    data = {"client_id": registration["client_id"], "token": result[hint], "token_type_hint": hint}
    assert (await client.post("/revoke", data=data)).status_code == 200
    assert (await client.post("/revoke", data=data)).status_code == 200
    assert not await provider.verify_token(result["access_token"])
    assert await provider.load_refresh_token(await provider.get_client(registration["client_id"]), result["refresh_token"]) is None


@pytest.mark.anyio
async def test_escaped_consent_does_not_render_client_html(client, provider):
    registration = await register(client, client_name='<script>alert("synthetic")</script>')
    _, page = await begin(client, registration)
    assert "<script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


@pytest.mark.anyio
@pytest.mark.parametrize("metadata", [
    {"redirect_uris": ["http://remote.example.test/callback"]},
    {"redirect_uris": ["https://user:password@example.test/callback"]},
    {"redirect_uris": [CALLBACK + "#fragment"]}, {"redirect_uris": ["https://*.example.test/callback"]},
    {"scope": "admin"}, {"grant_types": ["client_credentials"]},
    {"token_endpoint_auth_method": "private_key_jwt"}, {"jwks_uri": "https://keys.example.test/keys"},
])
async def test_registration_rejects_unsafe_or_unsupported_metadata(client, metadata):
    response = await client.post("/register", json={"redirect_uris": [CALLBACK], **metadata})
    assert response.status_code == 400


@pytest.mark.anyio
async def test_duplicate_parameters_bad_credentials_and_oversized_requests_fail_safely(client, provider, caplog):
    registration = await register(client, method="client_secret_post")
    code, _ = await approve(client, provider, registration)
    credential = registration["client_secret"]
    response = await client.post("/token", data={"client_id": registration["client_id"], "client_secret": credential + "wrong"})
    assert response.status_code == 401 and credential not in response.text
    request = urlencode({"grant_type": "authorization_code", "client_id": registration["client_id"], "client_secret": credential,
                         "code": code, "redirect_uri": CALLBACK, "code_verifier": VERIFIER, "resource": RESOURCE})
    response = await client.post("/token", content=request + "&resource=" + RESOURCE, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 400
    response = await client.post("/register", content='{"redirect_uris":[],"redirect_uris":[]}')
    assert response.status_code == 400
    response = await client.post("/token", headers=[("Authorization", "Basic synthetic"), ("Authorization", "Basic synthetic")], data={})
    assert response.status_code == 400
    response = await client.post("/register", content=b"x" * 16385)
    assert response.status_code == 413
    assert credential not in caplog.text


@pytest.mark.anyio
async def test_registration_rate_limit_and_pending_capacity_are_bounded(client, provider, monkeypatch):
    with provider._db() as db:
        db.execute("INSERT INTO rates VALUES ('/register', ?, 30)", (int(mcp_oauth.time.time()),))
    assert (await client.post("/register", json={"redirect_uris": [CALLBACK]})).status_code == 429
    with provider._db() as db:
        db.execute("DELETE FROM rates")
    registration = await register(client)
    monkeypatch.setattr(mcp_oauth, "MAX_PENDING", 0)
    response = await client.get("/authorize", params={"client_id": registration["client_id"], "response_type": "code", "code_challenge": CHALLENGE, "resource": RESOURCE})
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["temporarily_unavailable"]


def test_public_route_allowlist_exactly_matches_routes_and_methods(provider):
    routes = provider.create_routes()
    assert provider.public_routes == frozenset((method, route.path) for route in routes for method in route.methods)
    assert ("GET", "/register") not in provider.public_routes
    assert ("POST", "/mcp") not in provider.public_routes


@pytest.mark.parametrize("issuer", ["http://inventory.example.test", "https://user:pass@example.test", ISSUER + "/mcp", ISSUER + "?", ISSUER + "#", "https://*.example.test", ""])
def test_issuer_validation_never_creates_state_for_unsafe_origins(tmp_path, issuer):
    path = tmp_path / "oauth.sqlite3"
    with pytest.raises(ConfigurationError):
        OAuthProvider(path, issuer)
    assert not path.exists()


def test_state_is_bound_to_issuer_and_has_private_permissions(provider):
    with pytest.raises(ConfigurationError, match="different issuer"):
        OAuthProvider(provider.database_path, "https://another.example.test")
    assert OAuthProvider(provider.database_path, ISSUER).resource_url == RESOURCE
    if os.name != "nt":
        assert provider.database_path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("ttl", [0, -1, 601, True, "300"])
def test_owner_code_lifetime_is_bounded(provider, ttl):
    with pytest.raises(ConfigurationError, match="lifetime"):
        provider.issue_owner_code(ttl)


def test_operator_cli_outputs_only_single_use_code_and_safe_errors(tmp_path):
    result = subprocess.run([sys.executable, str(Path(mcp_oauth.__file__)), "owner-code", "--data-dir", str(tmp_path), "--issuer", ISSUER],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}\n", result.stdout)
    assert not result.stderr
    failure = subprocess.run([sys.executable, str(Path(mcp_oauth.__file__)), "owner-code", "--data-dir", str(tmp_path / "missing"), "--issuer", ISSUER],
                             capture_output=True, text=True, timeout=20)
    assert failure.returncode == 2 and not failure.stdout
    assert str(tmp_path) not in failure.stderr
