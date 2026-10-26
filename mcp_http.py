"""Authenticated, stateless Streamable HTTP for the existing inventory tools."""

from __future__ import annotations

import hmac
import ipaddress
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server import ConfigurationError, build_server
from storage import Storage

if TYPE_CHECKING:
    from mcp_oauth import OAuthProvider


_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_AUTHORIZATION = re.compile(rb"(?i:Bearer) ([A-Za-z0-9._~+/-]+=*)")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _hostname_is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_authority(value: str) -> str:
    """Accept an exact DNS/IP Host value, with an optional numeric port."""
    error = "HTTP allowed hosts must contain explicit hostnames or IP addresses with optional ports."
    if not isinstance(value, str) or not value or not value.isascii():
        raise ConfigurationError(error)
    if any(char.isspace() for char in value) or any(char in value for char in "/\\@?#%*"):
        raise ConfigurationError(error)
    try:
        parsed = urlsplit("//" + value)
        hostname, port = parsed.hostname, parsed.port
        if not hostname or parsed.path or parsed.query or parsed.fragment or value.endswith(":"):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if len(hostname) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in hostname.split(".")):
                raise ValueError from None
            canonical_hostname = hostname
        else:
            canonical_hostname = f"[{hostname}]" if address.version == 6 else hostname
        expected = canonical_hostname + (f":{port}" if port is not None else "")
        if expected.lower() != value.lower():
            raise ValueError
    except ValueError:
        raise ConfigurationError(error) from None
    return value.lower()


def validate_public_url(value: str | None) -> str | None:
    """Validate a public origin, permitting HTTP only on a loopback address."""
    if value is None:
        return None
    error = "MCP_PUBLIC_URL must be an HTTPS origin without credentials, query, fragment, or a non-root path."
    if not isinstance(value, str) or not value or not value.isascii():
        raise ConfigurationError(error)
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ConfigurationError(error)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError
        authority = _validate_authority(parsed.netloc)
        if parsed.scheme != "https" and not _hostname_is_loopback(parsed.hostname or ""):
            raise ValueError
    except ValueError:
        raise ConfigurationError(error) from None
    return f"{parsed.scheme}://{authority}"


class _BearerMiddleware:
    """Authorize before routing or reading a request body; never log credentials."""

    def __init__(self, app: ASGIApp, *, token: bytes, hosts: list[str], origins: list[str],
                 oauth: OAuthProvider | None = None) -> None:
        self.app = app
        self.token = token
        self.hosts = {host.encode("ascii") for host in hosts}
        self.origins = {origin.encode("ascii") for origin in origins}
        self.oauth = oauth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/healthz" and scope["method"] in {"GET", "HEAD"}:
            response = JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
            if scope["method"] == "HEAD":
                response.body = b""
            await response(scope, receive, send)
            return

        headers = scope.get("headers", [])
        hosts = [value for name, value in headers if name.lower() == b"host"]
        if self.oauth is not None and (scope["method"], scope["path"]) in self.oauth.public_routes:
            if len(hosts) != 1 or hosts[0] not in self.hosts:
                await Response("Invalid Host header", status_code=421)(scope, receive, send)
            else:
                await self.app(scope, receive, send)
            return
        authorization = [value for name, value in headers if name.lower() == b"authorization"]
        match = _AUTHORIZATION.fullmatch(authorization[0]) if len(authorization) == 1 else None
        authorized = match is not None and hmac.compare_digest(match.group(1), self.token)
        if not authorized and match is not None and self.oauth is not None:
            authorized = await self.oauth.verify_token(match.group(1).decode("ascii"))
        if not authorized:
            challenge = "Bearer"
            if self.oauth is not None:
                challenge += f' resource_metadata="{self.oauth.resource_metadata_url}"'
            response = Response(
                "Unauthorized", status_code=401,
                headers={"WWW-Authenticate": challenge, "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        # Reject ambiguous headers before the SDK examines their first value.
        # The SDK retains its own DNS-rebinding checks on the MCP endpoint.
        origins = [value for name, value in headers if name.lower() == b"origin"]
        if len(hosts) != 1 or hosts[0] not in self.hosts:
            response = Response("Invalid Host header", status_code=421)
        elif len(origins) > 1 or (origins and origins[0] not in self.origins):
            response = Response("Invalid Origin header", status_code=403)
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


def create_http_app(
    storage: Storage,
    user_id: int,
    token: str,
    *,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    oauth: OAuthProvider | None = None,
) -> Starlette:
    """Create the authenticated app, retaining the SDK's MCP ASGI lifespan."""
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise ConfigurationError("MCP_AUTH_TOKEN must be a nonempty ASCII bearer token without whitespace.")
    if not isinstance(allowed_hosts, list) or not allowed_hosts:
        raise ConfigurationError("HTTP requires at least one explicit allowed host.")
    hosts = list(dict.fromkeys(_validate_authority(host) for host in allowed_hosts))
    if not isinstance(allowed_origins, list):
        raise ConfigurationError("HTTP allowed origins must be an explicit list.")
    origins = []
    for origin in allowed_origins:
        validated = validate_public_url(origin)
        if validated is None or validated != origin:
            raise ConfigurationError("HTTP allowed origins must be exact origins without a trailing slash.")
        origins.append(validated)

    app = build_server(storage, user_id).streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )
    if oauth is not None:
        app.router.routes.extend(oauth.create_routes())
    app.add_middleware(_BearerMiddleware, token=token.encode("ascii"), hosts=hosts, origins=origins, oauth=oauth)
    return app


def serve_http(
    storage: Storage,
    user_id: int,
    token: str,
    *,
    host: str,
    port: int,
    public_url: str | None,
    oauth_database: Path | None = None,
) -> None:
    """Run HTTP with exact local/public allowlists and no request access log."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigurationError("PORT must be an integer between 1 and 65535.")
    bind_authority = f"[{host}]" if isinstance(host, str) and ":" in host else host
    _validate_authority(bind_authority)
    public_origin = validate_public_url(public_url)
    local_hosts = ["127.0.0.1", "localhost", "[::1]"]
    if _hostname_is_loopback(host) and bind_authority not in local_hosts:
        local_hosts.append(bind_authority)
    allowed_hosts = [f"{local}:{port}" for local in local_hosts]
    allowed_origins = [f"http://{local}:{port}" for local in local_hosts]
    if port == 80:
        allowed_hosts.extend(local_hosts)
        allowed_origins.extend(f"http://{local}" for local in local_hosts)
    if public_origin is not None:
        public = urlsplit(public_origin)
        allowed_hosts.append(public.netloc)
        allowed_hosts.append(public.netloc.rsplit(":", 1)[0] if public.port is not None else public.netloc)
        allowed_origins.append(public_origin)
        if (public.scheme, public.port) in {("https", 443), ("http", 80)}:
            allowed_origins.append(f"{public.scheme}://{allowed_hosts[-1]}")
    options = {"allowed_hosts": allowed_hosts, "allowed_origins": allowed_origins}
    if oauth_database is not None:
        if public_origin is None:
            raise ConfigurationError("OAuth requires a configured HTTPS public origin.")
        from mcp_oauth import OAuthProvider

        options["oauth"] = OAuthProvider(oauth_database, public_origin)
    app = create_http_app(storage, user_id, token, **options)
    uvicorn.run(app, host=host, port=port, access_log=False, log_level="info")
