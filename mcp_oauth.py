"""Local OAuth authorization for one configured inventory owner.

The MCP SDK owns protocol parsing and responses. This provider supplies private
state, resource-owner consent, and the small SDK compatibility guards needed by
public and confidential clients. It never opens the inventory database.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    OAuthAuthorizationServerProvider, RefreshToken, RegistrationError, TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import (
    build_metadata, cors_middleware, create_auth_routes, create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, Field, TypeAdapter, UrlConstraints
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server import ConfigurationError


SCOPE = "inventory"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
UNAPPROVED_CLIENT_TTL = 86400
APPROVED_CLIENT_EXPIRY = 0
CODE_TTL = 300
CONSENT_TTL = 600
MAX_ATTEMPTS = 5
MAX_CLIENTS = 1000
MAX_PENDING = 1000
MAX_GRANTS = 10000
MAX_REFRESH_HISTORY = 100000
_COOKIE = "__Host-warehouse-oauth-csrf"
_HEADERS = {
    # no-referrer makes navigation POSTs send Origin:null, including our own
    # consent form. Preserve same-origin checks without leaking cross-origin referrers.
    "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}
_TOOL_SUMMARY = (
    "Просматривать доступные дома", "Создавать дома", "Просматривать склады", "Создавать склады",
    "Просматривать запасы", "Добавлять товары и пополнять запасы", "Изменять количество товара",
    "Задавать количество товара", "Задавать минимальный запас", "Убирать позиции в архив",
    "Восстанавливать позиции из архива", "Составлять список покупок",
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _opaque() -> str:
    return secrets.token_urlsafe(32)


def _safe_redirect(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or "#" in value or "\\" in value or len(value) > 2048
                or re.fullmatch(r"[A-Za-z0-9._:\[\]-]+", parsed.netloc) is None
                or any(ord(char) < 33 or ord(char) > 126 for char in value)
                or parsed.netloc.endswith(":") or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            return False
        if parsed.scheme == "https":
            return True
        return parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError):
        return False


def _issuer(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (not _safe_redirect(value) or parsed.scheme != "https"
                or parsed.path not in {"", "/"} or "?" in value or "#" in value):
            raise ValueError
        return str(AnyHttpUrl(value)).rstrip("/")
    except (ValueError, TypeError):
        raise ConfigurationError("OAuth requires a root HTTPS issuer URL without credentials, query, or fragment.") from None


class _AccessToken(AccessToken):
    token: str = Field(repr=False)


class _RefreshToken(RefreshToken):
    token: str = Field(repr=False)


class _AuthorizationCode(AuthorizationCode):
    code: str = Field(repr=False)


class _TokenResponse(OAuthToken):
    access_token: str = Field(repr=False)
    refresh_token: str | None = Field(default=None, repr=False)


class _Database:
    # A class context manager preserves the SDK's frozen exception dataclasses;
    # contextlib's generator wrapper attempts to overwrite their traceback.
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row

    def __enter__(self):
        return self.connection

    def __exit__(self, kind, value, traceback):
        try:
            return self.connection.__exit__(kind, value, traceback)
        finally:
            self.connection.close()


class _HashedClientAuthenticator(ClientAuthenticator):
    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        form = await request.form()
        client_id = str(form.get("client_id", ""))
        client = await self.provider.get_client(client_id)
        if client is None:
            raise AuthenticationError("Invalid client credentials")
        provided = None
        if client.token_endpoint_auth_method == "client_secret_basic":
            try:
                authorization = request.headers.get("authorization", "")
                if authorization[:6].lower() != "basic ":
                    raise ValueError
                basic_id, provided = base64.b64decode(authorization[6:], validate=True).decode().split(":", 1)
                if unquote(basic_id) != client_id:
                    raise ValueError
                provided = unquote(provided)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                raise AuthenticationError("Invalid client credentials") from None
        elif client.token_endpoint_auth_method == "client_secret_post":
            provided = form.get("client_secret")
        elif client.token_endpoint_auth_method != "none":
            raise AuthenticationError("Invalid client credentials")
        with self.provider._db() as db:
            row = db.execute("SELECT secret_hash FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        if client.token_endpoint_auth_method != "none" and (
            row is None or not row["secret_hash"] or not isinstance(provided, str)
            or not hmac.compare_digest(_hash(provided), row["secret_hash"])
            or (client.client_secret_expires_at and client.client_secret_expires_at <= time.time())
        ):
            raise AuthenticationError("Invalid client credentials")
        return client


class _RequestGuard:
    """Bound public inputs before SDK parsing; enforce missing resource checks."""

    def __init__(self, app: ASGIApp, provider: OAuthProvider, path: str):
        self.app, self.provider, self.path = app, provider, path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        maximum, window = {
            "/register": (30, 3600), "/authorize": (120, 60), "/oauth/consent": (60, 60),
        }.get(self.path, (300, 60))
        if not self.provider._rate(self.path, maximum, window):
            await JSONResponse({"error": "temporarily_unavailable"}, 429, headers=_HEADERS)(scope, receive, send)
            return
        try:
            body = bytearray()
            if len(scope.get("query_string", b"")) > 8192:
                raise ValueError
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > (16384 if self.path == "/register" else 8192):
                    await JSONResponse({"error": "invalid_request"}, 413, headers=_HEADERS)(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            authorizations = [value for name, value in scope["headers"] if name.lower() == b"authorization"]
            if len(authorizations) > 1:
                raise ValueError
            if self.path == "/register":
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError
                        result[key] = value
                    return result

                if not isinstance(json.loads(body, object_pairs_hook=unique), dict):
                    raise ValueError
            else:
                if scope["method"] == "POST":
                    content_type = dict(scope["headers"]).get(b"content-type", b"").split(b";", 1)[0].lower()
                    if content_type != b"application/x-www-form-urlencoded":
                        raise ValueError
                encoded = scope.get("query_string", b"") if scope["method"] == "GET" else bytes(body)
                params = parse_qs(encoded.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
                if any(len(values) != 1 for values in params.values()):
                    raise ValueError
                if self.path in {"/token", "/revoke"} and "client_id" not in params and authorizations:
                    auth = authorizations[0]
                    if auth[:6].lower() == b"basic ":
                        basic_id, _ = base64.b64decode(auth[6:], validate=True).decode().split(":", 1)
                        body.extend(("&" + urlencode({"client_id": unquote(basic_id)})).encode())
                if self.path == "/token":
                    if "resource" in params and params["resource"] != [self.provider.resource_url]:
                        raise ValueError
                    if params.get("grant_type") == ["authorization_code"]:
                        if re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", params.get("code_verifier", [""])[0]) is None:
                            raise ValueError
                if self.path == "/revoke" and "client_secret" not in params:
                    body.extend(b"&client_secret=")
                if len(body) != len(encoded) and scope["method"] == "POST":
                    scope = dict(scope)
                    scope["headers"] = [(key, value) for key, value in scope["headers"] if key.lower() != b"content-length"]
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            await JSONResponse({"error": "invalid_request"}, 400, headers=_HEADERS)(scope, receive, send)
            return
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        # SDK validation descriptions may contain submitted values. Retain the
        # protocol error code and status without echoing credentials or forms.
        error_start, error_body = None, bytearray()

        async def safe_send(message):
            nonlocal error_start
            if self.path != "/oauth/consent" and message["type"] == "http.response.start" and message["status"] >= 400:
                error_start = message
                return
            if error_start is not None and message["type"] == "http.response.body":
                error_body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    try:
                        error = json.loads(error_body).get("error", "invalid_request")
                    except (ValueError, AttributeError):
                        error = "invalid_request"
                    if not isinstance(error, str) or re.fullmatch(r"[a-z_]+", error) is None:
                        error = "invalid_request"
                    response = JSONResponse({"error": error}, error_start["status"], headers=_HEADERS)
                    await response(scope, replay, send)
                return
            await send(message)

        await self.app(scope, replay, safe_send)


class OAuthProvider(OAuthAuthorizationServerProvider[_AuthorizationCode, _RefreshToken, _AccessToken]):
    def __init__(self, database_path: Path, issuer_url: str):
        self.issuer_url = _issuer(issuer_url)
        self.resource_url = self.issuer_url + "/mcp"
        self.resource_metadata_url = self.issuer_url + "/.well-known/oauth-protected-resource/mcp"
        self.database_path = Path(database_path).absolute()
        try:
            parent = self.database_path.parent
            if not parent.is_dir() or parent.resolve() != parent:
                raise OSError
            try:
                descriptor = os.open(self.database_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            with self._db() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS clients (
                        client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, secret_hash TEXT, expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS pending (
                        nonce_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
                        csrf_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS owner_codes (
                        code_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS codes (
                        code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, data TEXT NOT NULL, expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS grants (
                        access_hash TEXT PRIMARY KEY, refresh_hash TEXT UNIQUE, client_id TEXT NOT NULL,
                        scopes TEXT NOT NULL, resource TEXT NOT NULL, access_expires INTEGER NOT NULL,
                        refresh_expires INTEGER NOT NULL, family_hash TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS refresh_history (
                        refresh_hash TEXT PRIMARY KEY, family_hash TEXT NOT NULL, client_id TEXT NOT NULL,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS rates (category TEXT PRIMARY KEY, start INTEGER NOT NULL, count INTEGER NOT NULL);
                """)
                db.execute("INSERT OR IGNORE INTO settings VALUES ('issuer', ?)", (self.issuer_url,))
                if db.execute("SELECT value FROM settings WHERE key = 'issuer'").fetchone()[0] != self.issuer_url:
                    raise ConfigurationError("OAuth state is bound to a different issuer. Use the original issuer or separate OAuth state.")
                self._gc(db)
        except (OSError, sqlite3.Error):
            raise ConfigurationError("Cannot initialize the private OAuth database.") from None

    @property
    def public_routes(self) -> frozenset[tuple[str, str]]:
        routes = {(method, path) for path in (
            "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp",
        ) for method in ("GET", "HEAD", "OPTIONS")}
        routes.update((method, path) for path in ("/register", "/token", "/revoke") for method in ("POST", "OPTIONS"))
        routes.update((method, path) for path in ("/authorize", "/oauth/consent") for method in ("GET", "HEAD", "POST"))
        return frozenset(routes)

    def _db(self):
        info = self.database_path.lstat()
        if not stat.S_ISREG(info.st_mode) or self.database_path.is_symlink() or (os.name != "nt" and info.st_mode & 0o077):
            raise ConfigurationError("OAuth state must be a regular private file.")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.database_path.with_name(self.database_path.name + suffix)
            if sidecar.is_symlink():
                raise ConfigurationError("OAuth state sidecars must be regular private files.")
        return _Database(self.database_path)

    @staticmethod
    def _gc(db):
        now = int(time.time())
        # Client registrations outlive their owner-approved grants. Pin legacy
        # registrations before removing expired grant/code evidence as well.
        db.execute("""UPDATE clients SET expires_at = ? WHERE expires_at > 0 AND client_id IN (
            SELECT client_id FROM grants UNION SELECT client_id FROM codes
        )""", (APPROVED_CLIENT_EXPIRY,))
        for table in ("pending", "owner_codes", "codes", "refresh_history"):
            db.execute(f"DELETE FROM {table} WHERE expires_at <= ?", (now,))
        db.execute("DELETE FROM grants WHERE refresh_expires <= ?", (now,))
        db.execute("DELETE FROM clients WHERE expires_at > 0 AND expires_at <= ?", (now,))

    def _rate(self, category, maximum, window):
        now = int(time.time())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO rates VALUES (?, ?, 0)", (category, now))
            db.execute("UPDATE rates SET start = ?, count = 0 WHERE category = ? AND start <= ?", (now, category, now - window))
            row = db.execute("SELECT count FROM rates WHERE category = ?", (category,)).fetchone()
            if row[0] >= maximum:
                return False
            db.execute("UPDATE rates SET count = count + 1 WHERE category = ?", (category,))
        return True

    def issue_owner_code(self, ttl: int = 300) -> str:
        if type(ttl) is not int or not 1 <= ttl <= 600:
            raise ConfigurationError("Owner-code lifetime must be between 1 and 600 seconds.")
        code = _opaque()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._gc(db)
            db.execute("DELETE FROM owner_codes")
            db.execute("INSERT INTO owner_codes VALUES (?, ?, 0)", (_hash(code), int(time.time()) + ttl))
        return code

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if (not client_info.redirect_uris or len(client_info.redirect_uris) > 10
                or any(not _safe_redirect(str(uri)) for uri in client_info.redirect_uris)):
            raise RegistrationError("invalid_redirect_uri", "Use HTTPS or a literal loopback callback")
        if (client_info.token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic"}
                or "authorization_code" not in client_info.grant_types
                or not set(client_info.grant_types) <= {"authorization_code", "refresh_token"}
                or client_info.response_types != ["code"] or client_info.scope != SCOPE
                or client_info.jwks is not None or client_info.jwks_uri is not None
                or len(client_info.model_dump_json()) > 12000):
            raise RegistrationError("invalid_client_metadata", "Unsupported client metadata")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._gc(db)
            if db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "Registration capacity reached")
            db.execute("INSERT INTO clients VALUES (?, ?, ?, ?)", (
                client_info.client_id, client_info.model_copy(update={"client_secret": None}).model_dump_json(),
                _hash(client_info.client_secret) if client_info.client_secret else None, int(time.time()) + UNAPPROVED_CLIENT_TTL,
            ))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not isinstance(client_id, str) or len(client_id) > 256:
            return None
        with self._db() as db:
            self._gc(db)
            row = db.execute("SELECT metadata FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource != self.resource_url:
            raise AuthorizeError("invalid_target", "Invalid resource")
        if (re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge) is None
                or (params.state is not None and len(params.state) > 1024)
                or str(params.redirect_uri) not in [str(uri) for uri in client.redirect_uris or []]):
            raise AuthorizeError("invalid_request", "Invalid authorization parameters")
        if params.scopes is not None and params.scopes != [SCOPE]:
            raise AuthorizeError("invalid_scope", "Invalid scope")
        params = params.model_copy(update={"scopes": [SCOPE]})
        nonce = _opaque()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._gc(db)
            if db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] >= MAX_PENDING:
                raise AuthorizeError("temporarily_unavailable", "Authorization capacity reached")
            db.execute("INSERT INTO pending (nonce_hash, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
                       (_hash(nonce), client.client_id, params.model_dump_json(), int(time.time()) + CONSENT_TTL))
        return self.issuer_url + "/oauth/consent?" + urlencode({"request": nonce})

    async def load_authorization_code(self, client, authorization_code):
        if not isinstance(authorization_code, str) or len(authorization_code) > 256:
            return None
        with self._db() as db:
            row = db.execute("SELECT data FROM codes WHERE code_hash = ? AND client_id = ? AND expires_at > ?",
                             (_hash(authorization_code), client.client_id, time.time())).fetchone()
        return _AuthorizationCode(code=authorization_code, **json.loads(row[0])) if row else None

    async def exchange_authorization_code(self, client, authorization_code):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM codes WHERE code_hash = ? AND client_id = ? AND expires_at > ?",
                             (_hash(authorization_code.code), client.client_id, time.time())).fetchone()
            if row is None:
                raise TokenError("invalid_grant", "Invalid authorization code")
            data = json.loads(row[0])
            if data["resource"] != self.resource_url:
                raise TokenError("invalid_grant", "Invalid resource")
            db.execute("DELETE FROM codes WHERE code_hash = ?", (_hash(authorization_code.code),))
            return self._issue(db, client, data["scopes"])

    def _issue(self, db, client, scopes, family=None):
        # Rotation may temporarily leave no refreshable grant. Preserve the
        # already validated registration before garbage collection observes it.
        if db.execute("UPDATE clients SET expires_at = ? WHERE client_id = ?",
                      (APPROVED_CLIENT_EXPIRY, client.client_id)).rowcount != 1:
            raise TokenError("invalid_grant", "Invalid client")
        self._gc(db)
        if db.execute("SELECT COUNT(*) FROM grants").fetchone()[0] >= MAX_GRANTS:
            raise TokenError("invalid_grant", "Grant capacity reached")
        access, refresh = _opaque(), _opaque()
        now = int(time.time())
        allow_refresh = "refresh_token" in client.grant_types
        db.execute("INSERT INTO grants VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            _hash(access), _hash(refresh) if allow_refresh else None, client.client_id, json.dumps(scopes),
            self.resource_url, now + ACCESS_TTL, now + (REFRESH_TTL if allow_refresh else ACCESS_TTL), family or _hash(refresh),
        ))
        return _TokenResponse(access_token=access, refresh_token=refresh if allow_refresh else None,
                              expires_in=ACCESS_TTL, scope=" ".join(scopes))

    @staticmethod
    def _revoke_reused(db, token, client_id):
        row = db.execute("SELECT family_hash FROM refresh_history WHERE refresh_hash = ? AND client_id = ? AND expires_at > ?",
                         (_hash(token), client_id, time.time())).fetchone()
        if row:
            db.execute("DELETE FROM grants WHERE family_hash = ?", (row[0],))
            db.execute("DELETE FROM refresh_history WHERE family_hash = ?", (row[0],))

    async def load_refresh_token(self, client, refresh_token):
        if not isinstance(refresh_token, str) or len(refresh_token) > 256:
            return None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM grants WHERE refresh_hash = ? AND client_id = ? AND refresh_expires > ?",
                             (_hash(refresh_token), client.client_id, time.time())).fetchone()
            if row is None:
                self._revoke_reused(db, refresh_token, client.client_id)
        if row is None or row["resource"] != self.resource_url:
            return None
        return _RefreshToken(token=refresh_token, client_id=client.client_id, scopes=json.loads(row["scopes"]),
                             expires_at=row["refresh_expires"], resource=row["resource"], subject="inventory-owner")

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM grants WHERE refresh_hash = ? AND client_id = ? AND refresh_expires > ?",
                             (_hash(refresh_token.token), client.client_id, time.time())).fetchone()
            if row is None:
                self._revoke_reused(db, refresh_token.token, client.client_id)
            else:
                if row["resource"] != self.resource_url or scopes != [SCOPE]:
                    raise TokenError("invalid_grant", "Invalid resource or scope")
                self._gc(db)
                if db.execute("SELECT COUNT(*) FROM refresh_history").fetchone()[0] >= MAX_REFRESH_HISTORY:
                    raise TokenError("invalid_grant", "Refresh capacity reached")
                db.execute("INSERT INTO refresh_history VALUES (?, ?, ?, ?)",
                           (_hash(refresh_token.token), row["family_hash"], client.client_id, row["refresh_expires"]))
                # Refresh rotation retires the refresh credential, not access
                # tokens that other in-flight requests may still be using.
                db.execute("UPDATE grants SET refresh_hash = NULL, refresh_expires = access_expires WHERE refresh_hash = ?",
                           (_hash(refresh_token.token),))
                return self._issue(db, client, scopes, row["family_hash"])
        # Commit family revocation before raising the protocol error.
        raise TokenError("invalid_grant", "Invalid refresh token")

    async def load_access_token(self, token):
        if not isinstance(token, str) or len(token) > 256:
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM grants WHERE access_hash = ? AND access_expires > ?",
                             (_hash(token), time.time())).fetchone()
        if row is None or row["resource"] != self.resource_url or json.loads(row["scopes"]) != [SCOPE]:
            return None
        return _AccessToken(token=token, client_id=row["client_id"], scopes=[SCOPE], expires_at=row["access_expires"],
                            resource=row["resource"], subject="inventory-owner", claims={"iss": self.issuer_url})

    async def verify_token(self, token: str) -> bool:
        return await self.load_access_token(token) is not None

    async def revoke_token(self, token):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT family_hash FROM grants WHERE client_id = ? AND (access_hash = ? OR refresh_hash = ?)",
                             (token.client_id, _hash(token.token), _hash(token.token))).fetchone()
            if row:
                db.execute("DELETE FROM grants WHERE family_hash = ?", (row[0],))
                db.execute("DELETE FROM refresh_history WHERE family_hash = ?", (row[0],))

    async def _consent(self, request: Request) -> Response:
        if request.method in {"GET", "HEAD"}:
            nonce, csrf = request.query_params.get("request", ""), _opaque()
            with self._db() as db:
                self._gc(db)
                row = db.execute("SELECT * FROM pending WHERE nonce_hash = ?", (_hash(nonce),)).fetchone()
                if row is None or row["attempts"] >= MAX_ATTEMPTS:
                    return self._error_page("Запрос истёк. Начните подключение заново.")
                db.execute("UPDATE pending SET csrf_hash = ? WHERE nonce_hash = ?", (_hash(csrf), _hash(nonce)))
            params = AuthorizationParams.model_validate_json(row["params"])
            client = await self.get_client(row["client_id"])
            response = self._consent_page(params, nonce, csrf, client.client_name if client else None)
            response.set_cookie(_COOKIE, csrf, max_age=CONSENT_TTL, secure=True, httponly=True, samesite="lax", path="/")
            return response
        origins = request.headers.getlist("origin")
        if origins != [self.issuer_url]:
            return self._error_page("Проверка безопасности не пройдена.", 403)
        form = await request.form()
        nonce, csrf = str(form.get("request", "")), str(form.get("csrf", ""))
        cookies = request.headers.getlist("cookie")
        cookie = request.cookies.get(_COOKIE, "")
        if len(cookies) != 1 or not csrf or not cookie or not hmac.compare_digest(csrf.encode(), cookie.encode()):
            return self._error_page("Проверка безопасности не пройдена.", 403)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._gc(db)
            row = db.execute("SELECT * FROM pending WHERE nonce_hash = ?", (_hash(nonce),)).fetchone()
            if (row is None or row["attempts"] >= MAX_ATTEMPTS or not row["csrf_hash"]
                    or not hmac.compare_digest(_hash(csrf), row["csrf_hash"])):
                return self._error_page("Запрос истёк или проверка безопасности не пройдена.", 403)
            params = AuthorizationParams.model_validate_json(row["params"])
            if form.get("decision") == "deny":
                db.execute("DELETE FROM pending WHERE nonce_hash = ?", (_hash(nonce),))
                return self._finish(params, error="access_denied")
            if form.get("decision") != "allow":
                return self._error_page("Выберите действие.")
            db.execute("UPDATE pending SET attempts = attempts + 1 WHERE nonce_hash = ?", (_hash(nonce),))
            owner = db.execute("SELECT * FROM owner_codes WHERE code_hash = ? AND attempts < ?",
                               (_hash(str(form.get("code", ""))), MAX_ATTEMPTS)).fetchone()
            if owner is None:
                db.execute("UPDATE owner_codes SET attempts = attempts + 1")
                return self._error_page("Код недействителен или истёк. Получите новый код владельца.")
            if db.execute("SELECT COUNT(*) FROM codes").fetchone()[0] >= MAX_PENDING:
                return self._error_page("Слишком много подключений. Попробуйте позже.", 429)
            code = _opaque()
            data = params.model_dump(mode="json")
            data.pop("state", None)
            data.update(client_id=row["client_id"], expires_at=int(time.time()) + CODE_TTL, subject="inventory-owner")
            db.execute("UPDATE clients SET expires_at = ? WHERE client_id = ?",
                       (APPROVED_CLIENT_EXPIRY, row["client_id"]))
            db.execute("INSERT INTO codes VALUES (?, ?, ?, ?)", (_hash(code), row["client_id"], json.dumps(data), data["expires_at"]))
            db.execute("DELETE FROM owner_codes WHERE code_hash = ?", (owner["code_hash"],))
            db.execute("DELETE FROM pending WHERE nonce_hash = ?", (_hash(nonce),))
            return self._finish(params, code=code)

    @staticmethod
    def _consent_page(params, nonce, csrf, client_name):
        callback_url = urlsplit(str(params.redirect_uri))
        callback = callback_url.netloc
        callback_origin = f"{callback_url.scheme}://{callback}"
        # Chromium checks form-action across the POST's redirect chain. Allow
        # only this request's previously validated registered callback origin.
        headers = dict(_HEADERS)
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "form-action 'self';", f"form-action 'self' {callback_origin};",
        )
        fields = "".join(f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'
                         for name, value in (("request", nonce), ("csrf", csrf)))
        operations = "".join(f"<li>{html.escape(label)}</li>" for label in _TOOL_SUMMARY)
        page = (
            '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Доступ к домашним запасам</title><style>body{font:16px system-ui;max-width:620px;margin:40px auto;padding:20px;line-height:1.5}'
            'input,button{font:inherit;padding:10px;max-width:100%;box-sizing:border-box}label,input{display:block}button{margin:16px 8px 0 0}</style>'
            '<main><h1>Подключить домашние запасы</h1>'
            f'<p>Приложение: <strong>{html.escape(client_name or "MCP-клиент")}</strong><br>Адрес возврата: {html.escape(callback)}</p>'
            '<p>Приложение получит доступ к запасам настроенного владельца и сможет:</p>'
            f'<ul>{operations}</ul><form method="post" action="/oauth/consent">{fields}'
            '<label for="code">Одноразовый код владельца</label><input id="code" name="code" type="password" autocomplete="off" maxlength="128">'
            '<button type="submit" name="decision" value="allow">Разрешить доступ</button>'
            '<button type="submit" name="decision" value="deny">Отказать</button></form></main></html>'
        )
        return HTMLResponse(page, headers=headers)

    @staticmethod
    def _error_page(message, status=400):
        return HTMLResponse(f'<!doctype html><html lang="ru"><meta charset="utf-8"><p>{html.escape(message)}</p></html>',
                            status, headers=_HEADERS)

    def _finish(self, params, **values):
        response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), state=params.state,
                                    iss=self.issuer_url, **values), 302, headers=_HEADERS)
        response.delete_cookie(_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        return response

    def create_routes(self) -> list[Route]:
        return create_oauth_routes(self)


def create_oauth_routes(provider: OAuthProvider) -> list[Route]:
    options = ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE])
    revocation = RevocationOptions(enabled=True)
    issuer = TypeAdapter(Annotated[AnyHttpUrl, UrlConstraints(preserve_empty_path=True)]).validate_python(provider.issuer_url)
    routes = create_auth_routes(provider, issuer, client_registration_options=options, revocation_options=revocation)
    authenticator = _HashedClientAuthenticator(provider)
    handlers = {"/token": TokenHandler(provider, authenticator).handle, "/revoke": RevocationHandler(provider, authenticator).handle}
    metadata = build_metadata(issuer, None, options, revocation)
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
    metadata.revocation_endpoint_auth_methods_supported = metadata.token_endpoint_auth_methods_supported
    metadata.client_id_metadata_document_supported = False
    metadata.authorization_response_iss_parameter_supported = True

    async def metadata_response(request):
        return JSONResponse(metadata.model_dump(mode="json", exclude_none=True), headers=_HEADERS)

    for route in routes:
        if route.path == "/.well-known/oauth-authorization-server":
            route.app = cors_middleware(metadata_response, ["GET", "OPTIONS"])
        else:
            if route.path in handlers:
                route.app = cors_middleware(handlers[route.path], ["POST", "OPTIONS"])
            route.app = _RequestGuard(route.app, provider, route.path)
    consent = Route("/oauth/consent", provider._consent, methods=["GET", "POST"])
    consent.app = _RequestGuard(request_response(provider._consent), provider, consent.path)
    routes.append(consent)
    routes.extend(create_protected_resource_routes(AnyHttpUrl(provider.resource_url), [issuer],
                                                 scopes_supported=[SCOPE], resource_name="Домашние запасы"))
    return routes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a short-lived, single-use OAuth owner code.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    owner = subcommands.add_parser("owner-code")
    owner.add_argument("--data-dir", type=Path, required=True)
    owner.add_argument("--issuer", required=True)
    arguments = parser.parse_args(argv)
    try:
        provider = OAuthProvider(arguments.data_dir / "oauth.sqlite3", arguments.issuer)
        print(provider.issue_owner_code())
    except (ConfigurationError, OSError, sqlite3.Error):
        print("Cannot create an owner code. Check the private data directory and HTTPS issuer.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
