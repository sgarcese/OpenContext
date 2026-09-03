"""OAuth helpers for remote MCP connector authentication."""

from __future__ import annotations

import logging
import os
import secrets
import time
import base64
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import jwt
from jwt import PyJWKSet
from jwt.exceptions import InvalidTokenError, PyJWTError

logger = logging.getLogger(__name__)


DEFAULT_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
OAUTH_STATE_COOKIE_NAME = "opencontext_oauth_state"
LATEST_PENDING_KEY = "__latest__"
PENDING_IDP_STATE_PREFIX = "state:"


def _oauth_cookie_attributes(max_age: int) -> str:
    return f"Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite=None"


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    resource: str
    authorization_servers: List[str]
    scopes_supported: List[str]
    required_scopes: List[str]
    issuer: str
    jwks_uri: Optional[str]
    audience: Optional[str]
    algorithms: List[str]
    oauth_proxy_enabled: bool = False
    upstream_authorization_endpoint: Optional[str] = None
    upstream_token_endpoint: Optional[str] = None
    callback_url: Optional[str] = None
    # Confidential upstream IdP credentials (e.g. Strivacity). Claude talks to
    # this MCP as a public PKCE client; the proxy substitutes these upstream.
    upstream_client_id: Optional[str] = None
    upstream_client_secret: Optional[str] = None
    # Used to validate opaque (non-JWT) access tokens from the IdP.
    userinfo_endpoint: Optional[str] = None


@dataclass(frozen=True)
class AuthResult:
    valid: bool
    status_code: int = 401
    error: str = "invalid_token"
    description: str = "Missing or invalid bearer token"


def get_auth_config(config: Dict[str, Any]) -> AuthConfig:
    """Extract OAuth settings from the OpenContext config."""
    auth_config = config.get("auth", {})
    jwt_config = auth_config.get("jwt", {})
    oauth_proxy_config = auth_config.get("oauth_proxy", {})

    authorization_servers = auth_config.get("authorization_servers", [])
    if isinstance(authorization_servers, str):
        authorization_servers = [authorization_servers]

    scopes_supported = auth_config.get("scopes_supported", [])
    if isinstance(scopes_supported, str):
        scopes_supported = scopes_supported.split()

    required_scopes = auth_config.get("required_scopes", [])
    if isinstance(required_scopes, str):
        required_scopes = required_scopes.split()

    resolved_authorization_servers = [
        _resolve_env(str(server)) for server in authorization_servers
    ]

    algorithms = jwt_config.get("algorithms", ["RS256"])
    if isinstance(algorithms, str):
        algorithms = [algorithms]

    return AuthConfig(
        enabled=bool(auth_config.get("enabled", False)),
        resource=_resolve_env(str(auth_config.get("resource", ""))).rstrip("/"),
        authorization_servers=[
            server for server in resolved_authorization_servers if server
        ],
        scopes_supported=list(scopes_supported),
        required_scopes=list(required_scopes),
        issuer=_resolve_env(str(jwt_config.get("issuer", ""))),
        jwks_uri=_resolve_optional_env(jwt_config.get("jwks_uri")),
        audience=_resolve_optional_env(jwt_config.get("audience")),
        algorithms=list(algorithms),
        oauth_proxy_enabled=bool(oauth_proxy_config.get("enabled", False)),
        upstream_authorization_endpoint=_resolve_optional_env(
            oauth_proxy_config.get("authorization_endpoint")
        ),
        upstream_token_endpoint=_resolve_optional_env(
            oauth_proxy_config.get("token_endpoint")
        ),
        callback_url=_resolve_optional_env(oauth_proxy_config.get("callback_url")),
        upstream_client_id=_resolve_optional_env(
            oauth_proxy_config.get("client_id")
        ),
        upstream_client_secret=_resolve_optional_env(
            oauth_proxy_config.get("client_secret")
        ),
        userinfo_endpoint=_resolve_optional_env(
            auth_config.get("userinfo_endpoint")
            or jwt_config.get("userinfo_endpoint")
        ),
    )


def _resolve_env(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def _resolve_optional_env(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    resolved = _resolve_env(str(value))
    return resolved or None


def auth_is_enabled(config: Dict[str, Any]) -> bool:
    return get_auth_config(config).enabled


def is_protected_resource_metadata_path(path: str) -> bool:
    return path in {
        DEFAULT_PROTECTED_RESOURCE_PATH,
        f"{DEFAULT_PROTECTED_RESOURCE_PATH}/mcp",
    }


def is_authorization_server_metadata_path(path: str) -> bool:
    return path in {
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    }


def is_oauth_authorize_path(path: str) -> bool:
    return path == "/oauth2/auth"


def is_oauth_token_path(path: str) -> bool:
    return path == "/oauth2/token"


def is_oauth_register_path(path: str) -> bool:
    return path == "/oauth2/register"


def is_oauth_callback_path(path: str) -> bool:
    return path == "/oauth2/callback"


def is_oauth_continue_path(path: str) -> bool:
    return path == "/oauth2/continue"


def build_oauth_continue_url(auth_config: AuthConfig) -> str:
    issuer = auth_config.authorization_servers[0].rstrip("/")
    return f"{issuer}/oauth2/continue"


def is_jwks_path(path: str) -> bool:
    return path == "/.well-known/jwks.json"


def build_protected_resource_metadata(auth_config: AuthConfig) -> Dict[str, Any]:
    """Build the RFC 9728 protected resource metadata document."""
    metadata: Dict[str, Any] = {
        "resource": auth_config.resource,
        "authorization_servers": auth_config.authorization_servers,
        "bearer_methods_supported": ["header"],
    }

    if auth_config.scopes_supported:
        metadata["scopes_supported"] = auth_config.scopes_supported

    return metadata


def build_authorization_server_metadata(auth_config: AuthConfig) -> Dict[str, Any]:
    """Build OAuth authorization server metadata for Claude discovery."""
    issuer = auth_config.authorization_servers[0].rstrip("/")
    jwks_uri = (
        f"{issuer}/.well-known/jwks.json"
        if auth_config.oauth_proxy_enabled
        else auth_config.jwks_uri
    )
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth2/auth",
        "token_endpoint": f"{issuer}/oauth2/token",
        "registration_endpoint": f"{issuer}/oauth2/register",
        "jwks_uri": jwks_uri,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
            "none",
        ],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": auth_config.scopes_supported,
    }


def build_dynamic_client_registration_response(
    auth_config: AuthConfig, body: str
) -> Dict[str, Any]:
    """RFC 7591 Dynamic Client Registration response for Claude (public PKCE client).

    Claude registers against this MCP proxy AS. Upstream Home/Strivacity credentials
    stay server-side; Claude must not need a client secret in the connector UI.
    """
    try:
        requested = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid client registration JSON") from exc
    if not isinstance(requested, dict):
        raise ValueError("Client registration body must be a JSON object")

    redirect_uris = requested.get("redirect_uris") or [
        "https://claude.ai/api/mcp/auth_callback"
    ]
    if isinstance(redirect_uris, str):
        redirect_uris = [redirect_uris]
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise ValueError("redirect_uris is required")

    client_id = auth_config.upstream_client_id or secrets.token_urlsafe(16)
    client_name = requested.get("client_name") or "Claude"
    if not isinstance(client_name, str):
        client_name = "Claude"

    return {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "client_name": client_name,
        "redirect_uris": [str(uri) for uri in redirect_uris],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "code_challenge_methods": ["S256"],
        "scope": " ".join(auth_config.scopes_supported or ["openid"]),
    }


def build_metadata_url(resource: str) -> str:
    """Build the RFC 9728 metadata URL for the configured MCP resource."""
    parsed = urlparse(resource)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    resource_path = parsed.path.rstrip("/")

    if resource_path:
        return f"{origin}{DEFAULT_PROTECTED_RESOURCE_PATH}{resource_path}"

    return f"{origin}{DEFAULT_PROTECTED_RESOURCE_PATH}"


def build_www_authenticate_header(auth_config: AuthConfig) -> str:
    metadata_url = build_metadata_url(auth_config.resource)
    parts = [f'resource_metadata="{metadata_url}"']

    if auth_config.required_scopes:
        scope = " ".join(auth_config.required_scopes)
        parts.append(f'scope="{scope}"')

    return "Bearer " + ", ".join(parts)


def extract_bearer_token(headers: Dict[str, str]) -> Optional[str]:
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer" or not token.strip():
        return None

    return token.strip()


def _scope_set(scope_value: Any) -> set[str]:
    if isinstance(scope_value, str):
        return set(scope_value.split())
    if isinstance(scope_value, list):
        return {str(scope) for scope in scope_value}
    return set()


def _filter_scope(scope_value: str, allowed_scopes: List[str]) -> str:
    requested_scopes = scope_value.split()
    allowed = set(allowed_scopes)
    filtered = [scope for scope in requested_scopes if scope in allowed]
    return " ".join(filtered or allowed_scopes)


def _oauth_proxy_callback_url(auth_config: AuthConfig) -> str:
    if auth_config.callback_url:
        return auth_config.callback_url
    issuer = auth_config.authorization_servers[0].rstrip("/")
    return f"{issuer}/oauth2/callback"


def _encode_proxy_state(
    original_redirect_uri: str,
    claude_state: str,
    oauth_pending: Optional[Dict[str, str]] = None,
    idp_state: Optional[str] = None,
) -> str:
    if not claude_state:
        claude_state = secrets.token_urlsafe(32)
    if not idp_state:
        idp_state = secrets.token_urlsafe(32)
    payload: Dict[str, str] = {
        "redirect_uri": original_redirect_uri,
        "state": claude_state,
        "idp_state": idp_state,
    }
    if oauth_pending:
        for key in ("client_id", "code_challenge", "code_challenge_method"):
            value = oauth_pending.get(key)
            if isinstance(value, str) and value:
                payload[key] = value
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _idp_state_from_decoded(decoded_state: Dict[str, str]) -> str:
    return decoded_state.get("idp_state") or decoded_state["state"]


def _claude_state_from_decoded(decoded_state: Dict[str, str]) -> str:
    return decoded_state["state"]


def _session_id_from_redirect_uri(redirect_uri: str) -> Optional[str]:
    if not redirect_uri:
        return None
    session_id = dict(parse_qsl(urlparse(redirect_uri).query)).get("session_id")
    return session_id if session_id else None


def _decode_proxy_state(proxy_state: str) -> Dict[str, str]:
    padding = "=" * (-len(proxy_state) % 4)
    payload = base64.urlsafe_b64decode(f"{proxy_state}{padding}".encode("ascii"))
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("OAuth proxy state payload must be an object")
    redirect_uri = decoded.get("redirect_uri")
    state = decoded.get("state")
    if not isinstance(redirect_uri, str) or not isinstance(state, str):
        raise ValueError("OAuth proxy state payload is missing required fields")
    result: Dict[str, str] = {"redirect_uri": redirect_uri, "state": state}
    for key in (
        "idp_state",
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "authorize_url",
    ):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


def _append_query_params(url: str, params: Dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _build_state_cookie(proxy_state: str) -> str:
    return f"{OAUTH_STATE_COOKIE_NAME}={proxy_state}; {_oauth_cookie_attributes(600)}"


def _expire_state_cookie() -> str:
    return f"{OAUTH_STATE_COOKIE_NAME}=; {_oauth_cookie_attributes(0)}"


def _client_id_from_basic_auth(headers: Dict[str, str]) -> Optional[str]:
    authorization = headers.get("authorization") or headers.get("Authorization") or ""
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "basic" or not credentials:
        return None
    try:
        decoded = base64.b64decode(credentials).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    client_id, _, _ = decoded.partition(":")
    return client_id or None


def _get_cookie_value(headers: Optional[Dict[str, str]], cookie_name: str) -> str:
    if not headers:
        return ""
    cookie_header = headers.get("cookie") or headers.get("Cookie") or ""
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == cookie_name:
            return value
    return ""


def callback_query_params(query_string: str) -> Dict[str, str]:
    return dict(parse_qsl(query_string, keep_blank_values=True))


def classify_oauth_callback(query_string: str) -> str:
    params = callback_query_params(query_string)
    if params.get("error"):
        return "idp_error"
    if params.get("code"):
        return "authorization_code"
    if callback_is_claude_session_bootstrap(query_string):
        return "claude_session_bootstrap"
    if callback_is_claude_session_only(query_string):
        return "claude_session_only"
    if params.get("state"):
        return "state_without_code"
    return "empty"


def build_callback_diagnostic_error(
    query_string: str,
    path: str,
    method: str,
    headers: Dict[str, str],
    reason: str,
) -> Dict[str, Any]:
    params = callback_query_params(query_string)
    safe_params = {
        key: value
        for key, value in params.items()
        if key not in {"code", "access_token", "id_token"}
    }
    body: Dict[str, Any] = {
        "error": "missing_authorization_code",
        "error_description": reason,
        "method": method,
        "path": path,
        "query_params": safe_params,
        "query_keys": sorted(params.keys()),
        "has_code": bool(params.get("code")),
        "has_error": bool(params.get("error")),
        "has_state": bool(params.get("state")),
        "callback_kind": classify_oauth_callback(query_string),
        "headers_subset": {
            "referer": headers.get("referer"),
            "user-agent": headers.get("user-agent"),
        },
    }
    if params.get("error"):
        body["idp_error"] = params["error"]
        if params.get("error_description"):
            body["idp_error_description"] = params["error_description"]
    return body


def has_oauth_authorize_params(query_string: str) -> bool:
    params = callback_query_params(query_string)
    return bool(
        params.get("response_type") == "code"
        and params.get("client_id")
        and params.get("redirect_uri")
        and params.get("state")
        and params.get("code_challenge")
        and params.get("code_challenge_method")
    )


def authorize_is_claude_bootstrap_only(query_string: str) -> bool:
    """Session bootstrap hit /oauth2/auth without OAuth parameters."""
    if has_oauth_authorize_params(query_string):
        return False
    params = callback_query_params(query_string)
    return any(key in params for key in ("session_id", "short_app_id", "language"))


def _is_upstream_authorize_url(auth_config: AuthConfig, url: str) -> bool:
    if not auth_config.upstream_authorization_endpoint:
        return False
    parsed = urlparse(url)
    upstream = urlparse(auth_config.upstream_authorization_endpoint)
    if parsed.netloc != upstream.netloc or parsed.path != upstream.path:
        return False
    params = callback_query_params(parsed.query)
    return bool(params.get("client_id") and params.get("response_type") == "code")


def rebuild_pending_authorize_url(
    auth_config: AuthConfig, decoded_state: Dict[str, str]
) -> Optional[str]:
    legacy_url = decoded_state.get("authorize_url")
    if legacy_url and _is_upstream_authorize_url(auth_config, legacy_url):
        return legacy_url

    required = ("client_id", "code_challenge", "code_challenge_method", "state")
    if not all(decoded_state.get(key) for key in required):
        return None
    if not auth_config.upstream_authorization_endpoint:
        return None

    params = {
        "client_id": auth_config.upstream_client_id or decoded_state["client_id"],
        "response_type": "code",
        "redirect_uri": _oauth_proxy_callback_url(auth_config),
        "state": _idp_state_from_decoded(decoded_state),
        "code_challenge": decoded_state["code_challenge"],
        "code_challenge_method": decoded_state["code_challenge_method"],
        "response_mode": "query",
        "scope": _filter_scope(
            "", auth_config.scopes_supported or ["openid"]
        ),
    }
    return (
        f"{auth_config.upstream_authorization_endpoint}?{urlencode(params)}"
    )


def get_valid_pending_authorize_url(
    auth_config: AuthConfig,
    headers: Optional[Dict[str, str]],
    session_id: Optional[str] = None,
) -> Optional[str]:
    cookie_state = _get_cookie_value(headers, OAUTH_STATE_COOKIE_NAME)
    if cookie_state:
        try:
            decoded_state = _decode_proxy_state(cookie_state)
            url = rebuild_pending_authorize_url(auth_config, decoded_state)
            if url:
                return url
        except Exception:
            pass

    from server.oauth_pending_store import (
        get_pending_proxy_state,
        save_pending_proxy_state,
    )

    if session_id:
        stored_state = get_pending_proxy_state(session_id)
        if not stored_state:
            stored_state = get_pending_proxy_state(LATEST_PENDING_KEY)
            if stored_state:
                save_pending_proxy_state(session_id, stored_state)
        if stored_state:
            try:
                decoded_state = _decode_proxy_state(stored_state)
                return rebuild_pending_authorize_url(auth_config, decoded_state)
            except Exception:
                return None

    stored_state = get_pending_proxy_state(LATEST_PENDING_KEY)
    if stored_state:
        try:
            decoded_state = _decode_proxy_state(stored_state)
            return rebuild_pending_authorize_url(auth_config, decoded_state)
        except Exception:
            return None
    return None


def _pending_lookup_keys(
    original_redirect_uri: str, idp_state: str
) -> List[str]:
    keys = [
        f"{PENDING_IDP_STATE_PREFIX}{idp_state}",
        LATEST_PENDING_KEY,
    ]
    session_id = _session_id_from_redirect_uri(original_redirect_uri)
    if session_id:
        keys.insert(0, session_id)
    return keys


def persist_authorization_pending(
    original_redirect_uri: str, proxy_state: str, idp_state: str
) -> None:
    """Store pending OAuth state for browser bootstrap and IdP callback recovery."""
    if not proxy_state or not idp_state:
        return
    from server.oauth_pending_store import save_pending_proxy_state

    for key in _pending_lookup_keys(original_redirect_uri, idp_state):
        save_pending_proxy_state(key, proxy_state)


def _resolve_proxy_state_blob(
    headers: Optional[Dict[str, str]], returned_idp_state: str
) -> str:
    cookie_state = _get_cookie_value(headers, OAUTH_STATE_COOKIE_NAME)
    if cookie_state:
        return cookie_state
    if returned_idp_state:
        from server.oauth_pending_store import get_pending_proxy_state

        stored = get_pending_proxy_state(
            f"{PENDING_IDP_STATE_PREFIX}{returned_idp_state}"
        )
        if stored:
            return stored
    raise ValueError("OAuth callback state is missing or expired")


def clear_authorization_pending(
    session_id: Optional[str] = None,
    returned_idp_state: Optional[str] = None,
) -> None:
    from server.oauth_pending_store import delete_pending_proxy_state

    if returned_idp_state:
        delete_pending_proxy_state(
            f"{PENDING_IDP_STATE_PREFIX}{returned_idp_state}"
        )
    if session_id:
        delete_pending_proxy_state(session_id)


def build_claude_connector_help_page(
    resource_url: str,
    hit_path: str,
    continue_url: Optional[str] = None,
) -> str:
    continue_block = ""
    if continue_url:
        continue_block = (
            f'<p><a href="{continue_url}">Continue to Strivacity sign-in</a></p>'
        )
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Connector setup</title></head>
<body>
<h1>Wrong URL for Claude connector</h1>
<p>You opened <code>{hit_path}</code> with Claude session parameters only.</p>
<p>This path is not the MCP connector URL and does not start OAuth by itself.</p>
<p><strong>Configure the custom connector with:</strong></p>
<p><code>{resource_url}</code></p>
<p>Claude will discover OAuth from MCP metadata and call <code>/oauth2/auth</code>
with <code>client_id</code>, <code>state</code>, and PKCE parameters automatically.</p>
{continue_block}
</body>
</html>"""


def get_pending_authorize_url(headers: Optional[Dict[str, str]]) -> Optional[str]:
    """Deprecated: use get_valid_pending_authorize_url with AuthConfig."""
    cookie_state = _get_cookie_value(headers, OAUTH_STATE_COOKIE_NAME)
    if not cookie_state:
        return None
    try:
        decoded_state = _decode_proxy_state(cookie_state)
    except Exception:
        return None
    return decoded_state.get("authorize_url")


def build_no_pending_auth_page(resource_url: str) -> str:
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Authorization setup</title></head>
<body>
<p>No pending authorization request.</p>
<p>Click <strong>Connect</strong> on the MCP connector, then use
<strong>Continue to Strivacity sign-in</strong> on this page.</p>
<p>Connector URL:</p>
<p><code>{resource_url}</code></p>
</body>
</html>"""


def build_claude_session_callback_error_page(resource_url: str) -> str:
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Wrong OAuth callback URL</title></head>
<body>
<p>Authorization was opened at the wrong callback URL.</p>
<p>Start from the MCP connector URL:</p>
<p><code>{resource_url}</code></p>
<p>Do not open <code>/oauth2/callback</code> directly — Strivacity returns here with
<code>code</code> and <code>state</code> after sign-in.</p>
</body>
</html>"""


def build_authorization_waiting_page(
    sign_in_url: Optional[str] = None,
    auto_redirect: bool = False,
) -> str:
    auto_redirect_block = ""
    link = ""
    button = ""
    if sign_in_url:
        escaped_url = sign_in_url.replace("'", "\\'")
        if auto_redirect:
            auto_redirect_block = (
                f'<meta http-equiv="refresh" content="0;url={sign_in_url}">'
                f'<script>window.top.location.replace("{sign_in_url}");</script>'
            )
        link = (
            f'<p><a href="{sign_in_url}" target="_top" rel="noopener">'
            "Continue to Strivacity sign-in</a></p>"
        )
        button = (
            f'<p><button type="button" onclick="window.top.location.assign('
            f"'{escaped_url}')\">Continue to Strivacity sign-in</button></p>"
        )
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Authorization in progress</title>{auto_redirect_block}</head>
<body>
<p>Authorization is in progress.</p>
<p>Click below to open Strivacity sign-in.</p>
{link}
{button}
</body>
</html>"""


def callback_is_claude_session_bootstrap(query_string: str) -> bool:
    """Claude opened /oauth2/callback as a session page — not an IdP OAuth response."""
    params = callback_query_params(query_string)
    if any(key in params for key in ("code", "error", "state")):
        return False
    return {"session_id", "short_app_id"} <= set(params.keys())


def callback_is_claude_session_only(query_string: str) -> bool:
    """Claude session params on the proxy callback without an OAuth response."""
    params = callback_query_params(query_string)
    if any(key in params for key in ("code", "error", "state")):
        return False
    return any(key in params for key in ("session_id", "short_app_id", "language"))


def callback_has_oauth_response(query_string: str) -> bool:
    params = dict(parse_qsl(query_string, keep_blank_values=True))
    return bool(params.get("code") or params.get("error"))


def callback_needs_fragment_recovery(query_string: str) -> bool:
    params = dict(parse_qsl(query_string, keep_blank_values=True))
    return not any(key in params for key in ("code", "error", "state"))


def build_fragment_recovery_page() -> str:
    return """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Completing authorization...</title></head>
<body>
<p>Completing authorization...</p>
<script>
(function () {
  var attempts = 0;
  function tryComplete() {
    var hash = window.location.hash ? window.location.hash.substring(1) : "";
    if (hash) {
      var current = new URL(window.location.href);
      var params = new URLSearchParams(current.search);
      var fragment = new URLSearchParams(hash);
      fragment.forEach(function (value, key) {
        if (!params.has(key)) params.set(key, value);
      });
      current.hash = "";
      current.search = params.toString();
      window.location.replace(current.toString());
      return;
    }
    attempts += 1;
    if (attempts < 20) {
      setTimeout(tryComplete, 250);
      return;
    }
    document.body.textContent = "Authorization failed: missing OAuth response.";
  }
  tryComplete();
})();
</script>
</body>
</html>"""


def build_authorization_redirect(
    auth_config: AuthConfig, query_string: str
) -> tuple[str, Optional[str]]:
    """Build the upstream authorization redirect URL with unsupported scopes removed."""
    if not auth_config.upstream_authorization_endpoint:
        raise ValueError("OAuth proxy authorization endpoint is not configured")

    params = dict(parse_qsl(query_string, keep_blank_values=True))
    params.pop("resource", None)
    for key in (
        "session_id",
        "short_app_id",
        "language",
        "opencontext_from_callback",
    ):
        params.pop(key, None)
    original_redirect_uri = params.get("redirect_uri", "")
    claude_state = params.get("state", "") or secrets.token_urlsafe(32)
    idp_state = secrets.token_urlsafe(32)
    state_cookie = None
    if auth_config.upstream_client_id:
        params["client_id"] = auth_config.upstream_client_id
    if original_redirect_uri:
        params["redirect_uri"] = _oauth_proxy_callback_url(auth_config)
        params["state"] = idp_state
    params.setdefault("response_type", "code")
    params["response_mode"] = "query"
    params["scope"] = _filter_scope(
        params.get("scope", ""), auth_config.scopes_supported or ["openid"]
    )
    redirect_url = (
        f"{auth_config.upstream_authorization_endpoint}?{urlencode(params)}"
    )
    if original_redirect_uri:
        proxy_state = _encode_proxy_state(
            original_redirect_uri,
            claude_state,
            oauth_pending={
                "client_id": params.get("client_id", ""),
                "code_challenge": params.get("code_challenge", ""),
                "code_challenge_method": params.get("code_challenge_method", ""),
            },
            idp_state=idp_state,
        )
        state_cookie = _build_state_cookie(proxy_state)
        persist_authorization_pending(original_redirect_uri, proxy_state, idp_state)
        logger.info(
            "OAuth authorization redirect built",
            extra={
                "claude_state_length": len(claude_state),
                "idp_state_length": len(idp_state),
                "client_id": params.get("client_id"),
                "code_challenge_method": params.get("code_challenge_method"),
                "upstream_host": urlparse(
                    auth_config.upstream_authorization_endpoint or ""
                ).netloc,
                "proxy_callback_url": _oauth_proxy_callback_url(auth_config),
                "claude_redirect_host": urlparse(original_redirect_uri).netloc,
                "claude_session_id": _session_id_from_redirect_uri(original_redirect_uri),
                "filtered_scope": params.get("scope"),
            },
        )
    return redirect_url, state_cookie


def build_callback_redirect(
    query_string: str, headers: Optional[Dict[str, str]] = None
) -> tuple[str, str]:
    params = dict(parse_qsl(query_string, keep_blank_values=True))
    returned_idp_state = params.get("state", "")
    try:
        proxy_state_blob = _resolve_proxy_state_blob(headers, returned_idp_state)
        decoded_state = _decode_proxy_state(proxy_state_blob)
    except Exception:
        # Some providers preserve Claude's callback query params but not our wrapped
        # state. In that case, forward the raw OAuth response back to Claude.
        if (
            returned_idp_state
            and any(key in params for key in ("session_id", "short_app_id", "language"))
        ):
            claude_params = {
                key: value
                for key, value in params.items()
                if key
                in {
                    "code",
                    "error",
                    "error_description",
                    "error_uri",
                    "session_id",
                    "short_app_id",
                    "language",
                }
            }
            claude_params["state"] = returned_idp_state
            return (
                _append_query_params(
                    "https://claude.ai/api/mcp/auth_callback", claude_params
                ),
                _expire_state_cookie(),
            )
        raise

    expected_idp_state = _idp_state_from_decoded(decoded_state)
    if returned_idp_state and returned_idp_state != expected_idp_state:
        raise ValueError("OAuth callback IdP state mismatch")

    claude_state = _claude_state_from_decoded(decoded_state)
    callback_params: Dict[str, str] = {"state": claude_state}
    for key in (
        "code",
        "error",
        "error_description",
        "error_uri",
    ):
        if key in params:
            callback_params[key] = params[key]

    return (
        _append_query_params(decoded_state["redirect_uri"], callback_params),
        _expire_state_cookie(),
    )


async def proxy_jwks_request(
    auth_config: AuthConfig,
    timeout_seconds: float = 10.0,
) -> tuple[int, Dict[str, str], str]:
    """Proxy JWKS metadata for OAuth discovery on the MCP issuer domain."""
    if not auth_config.jwks_uri:
        raise ValueError("OAuth JWKS URI is not configured")

    jwks = await _fetch_jwks(auth_config.jwks_uri, timeout_seconds)
    return (
        200,
        {"Content-Type": "application/json"},
        json.dumps(jwks),
    )


async def proxy_token_request(
    auth_config: AuthConfig,
    body: str,
    headers: Dict[str, str],
    timeout_seconds: float = 10.0,
) -> tuple[int, Dict[str, str], str]:
    """Proxy OAuth token requests to the upstream provider."""
    if not auth_config.upstream_token_endpoint:
        raise ValueError("OAuth proxy token endpoint is not configured")

    form = dict(parse_qsl(body, keep_blank_values=True))
    form.pop("resource", None)
    # Drop any secret Claude (or a browser) sent; inject the upstream IdP secret.
    form.pop("client_secret", None)
    if "redirect_uri" in form:
        form["redirect_uri"] = _oauth_proxy_callback_url(auth_config)
    if "scope" in form:
        form["scope"] = _filter_scope(
            form["scope"], auth_config.scopes_supported or ["openid"]
        )

    proxy_headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Home/Strivacity clients are typically client_secret_basic (not _post).
    if auth_config.upstream_client_id and auth_config.upstream_client_secret:
        basic = base64.b64encode(
            f"{auth_config.upstream_client_id}:{auth_config.upstream_client_secret}".encode(
                "utf-8"
            )
        ).decode("ascii")
        proxy_headers["Authorization"] = f"Basic {basic}"
        form["client_id"] = auth_config.upstream_client_id
        form.pop("client_secret", None)
    else:
        if auth_config.upstream_client_id:
            form["client_id"] = auth_config.upstream_client_id
        elif "client_id" not in form:
            client_id = _client_id_from_basic_auth(headers)
            if client_id:
                form["client_id"] = client_id
        if auth_config.upstream_client_secret:
            form["client_secret"] = auth_config.upstream_client_secret

    logger.info(
        "OAuth upstream token request",
        extra={
            "upstream_url": auth_config.upstream_token_endpoint,
            "form_keys": sorted(form.keys()),
            "grant_type": form.get("grant_type"),
            "has_code_verifier": bool(form.get("code_verifier")),
            "has_code": bool(form.get("code")),
            "has_client_secret": bool(form.get("client_secret")),
            "uses_client_secret_basic": "Authorization" in proxy_headers,
            "redirect_uri_host": urlparse(form.get("redirect_uri", "")).netloc
            if form.get("redirect_uri")
            else None,
            "client_id": form.get("client_id"),
        },
    )

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            auth_config.upstream_token_endpoint,
            data=form,
            headers=proxy_headers,
        )

    response_summary: Dict[str, Any] = {
        "upstream_url": auth_config.upstream_token_endpoint,
        "upstream_status": response.status_code,
        "upstream_content_type": response.headers.get("content-type"),
        "upstream_body_length": len(response.text),
    }
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            response_summary["upstream_error"] = parsed.get("error")
            response_summary["upstream_error_description"] = parsed.get(
                "error_description"
            )
            response_summary["upstream_has_access_token"] = bool(
                parsed.get("access_token")
            )
    except json.JSONDecodeError:
        response_summary["upstream_body_preview"] = response.text[:200]

    logger.info("OAuth upstream token response", extra=response_summary)

    return (
        response.status_code,
        {"Content-Type": response.headers.get("content-type", "application/json")},
        response.text,
    )


_JWKS_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
JWKS_CACHE_TTL_SECONDS = 300


async def _fetch_jwks(jwks_uri: str, timeout_seconds: float) -> Dict[str, Any]:
    now = time.time()
    cached = _JWKS_CACHE.get(jwks_uri)
    if cached and cached[0] > now:
        return cached[1]

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        logger.info(
            "OAuth upstream JWKS request",
            extra={"upstream_url": jwks_uri},
        )
        response = await client.get(jwks_uri, headers={"Accept": "application/json"})
    logger.info(
        "OAuth upstream JWKS response",
        extra={
            "upstream_url": jwks_uri,
            "upstream_status": response.status_code,
            "upstream_body_length": len(response.text),
        },
    )
    response.raise_for_status()
    jwks = response.json()
    _JWKS_CACHE[jwks_uri] = (now + JWKS_CACHE_TTL_SECONDS, jwks)
    return jwks


def _select_signing_key(token: str, jwks: Dict[str, Any]):
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    key_set = PyJWKSet.from_dict(jwks)

    for key in key_set.keys:
        if key.key_id == kid:
            return key.key

    raise InvalidTokenError("No matching JWKS key found for token kid")


def _looks_like_jwt(token: str) -> bool:
    """OIDC access tokens may be JWTs or opaque strings; JWTs have 3 segments."""
    return token.count(".") == 2 and all(token.split("."))


async def _validate_opaque_access_token(
    token: str,
    auth_config: AuthConfig,
    timeout_seconds: float,
) -> AuthResult:
    """Validate an opaque access token by calling the IdP userinfo endpoint."""
    if not auth_config.userinfo_endpoint:
        return AuthResult(
            valid=False,
            status_code=500,
            error="server_error",
            description=(
                "Opaque access token received but userinfo endpoint is not configured"
            ),
        )

    logger.info(
        "OAuth opaque token userinfo validation",
        extra={
            "userinfo_url": auth_config.userinfo_endpoint,
            "token_length": len(token),
        },
    )
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                auth_config.userinfo_endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("OAuth userinfo request failed: %s", exc, exc_info=True)
        return AuthResult(
            valid=False,
            error="invalid_token",
            description="Userinfo validation failed",
        )

    logger.info(
        "OAuth opaque token userinfo response",
        extra={
            "userinfo_url": auth_config.userinfo_endpoint,
            "userinfo_status": response.status_code,
        },
    )

    if response.status_code != 200:
        return AuthResult(
            valid=False,
            error="invalid_token",
            description="Access token was rejected by userinfo",
        )

    # Successful userinfo means the access token is active. Scope was already
    # constrained at authorize/token time by the OAuth proxy.
    return AuthResult(valid=True, status_code=200, error="", description="")


async def validate_bearer_token(
    token: str,
    auth_config: AuthConfig,
    timeout_seconds: float = 10.0,
) -> AuthResult:
    """Validate a bearer token (JWT via JWKS, or opaque via userinfo)."""
    if not _looks_like_jwt(token):
        return await _validate_opaque_access_token(
            token, auth_config, timeout_seconds
        )

    if not auth_config.issuer:
        return AuthResult(
            valid=False,
            status_code=500,
            error="server_error",
            description="OAuth JWT issuer is not configured",
        )

    if not auth_config.jwks_uri:
        return AuthResult(
            valid=False,
            status_code=500,
            error="server_error",
            description="OAuth JWKS URI is not configured",
        )

    try:
        jwks = await _fetch_jwks(auth_config.jwks_uri, timeout_seconds)
        signing_key = _select_signing_key(token, jwks)
        decode_kwargs: Dict[str, Any] = {
            "key": signing_key,
            "algorithms": auth_config.algorithms,
            "issuer": auth_config.issuer,
            "options": {"verify_aud": auth_config.audience is not None},
        }
        if auth_config.audience:
            decode_kwargs["audience"] = auth_config.audience

        payload = jwt.decode(token, **decode_kwargs)
    except (httpx.HTTPError, json.JSONDecodeError, PyJWTError, InvalidTokenError) as exc:
        logger.warning("OAuth JWT validation failed: %s", exc, exc_info=True)
        return AuthResult(
            valid=False,
            error="invalid_token",
            description="JWT validation failed",
        )

    if "exp" in payload and int(payload["exp"]) <= int(time.time()):
        return AuthResult(
            valid=False,
            error="invalid_token",
            description="Token is expired",
        )

    token_scopes = _scope_set(payload.get("scope"))
    missing_scopes = set(auth_config.required_scopes) - token_scopes
    if missing_scopes:
        return AuthResult(
            valid=False,
            error="insufficient_scope",
            description="Token is missing required scopes",
        )

    audiences = payload.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]
    if auth_config.resource and audiences and auth_config.resource not in audiences:
        return AuthResult(
            valid=False,
            error="invalid_token",
            description="Token audience does not match this MCP resource",
        )

    return AuthResult(valid=True, status_code=200, error="", description="")
