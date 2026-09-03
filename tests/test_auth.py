import os
import json
import time
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from server.auth import (
    AuthConfig,
    _JWKS_CACHE,
    build_authorization_redirect,
    build_authorization_server_metadata,
    build_callback_redirect,
    build_fragment_recovery_page,
    build_protected_resource_metadata,
    build_www_authenticate_header,
    callback_needs_fragment_recovery,
    get_auth_config,
    proxy_token_request,
    validate_bearer_token,
)


def test_get_auth_config_resolves_environment_values():
    with patch.dict(
        os.environ,
        {
            "OAUTH_ISSUER": "https://auth.example.gov",
            "OAUTH_JWKS_URI": "https://auth.example.gov/.well-known/jwks.json",
            "OAUTH_AUDIENCE": "https://data-mcp-staging.boston.gov/mcp",
            "OAUTH_CLIENT_ID": "upstream-client",
            "OAUTH_CLIENT_SECRET": "upstream-secret",
        },
    ):
        auth_config = get_auth_config(
            {
                "auth": {
                    "enabled": True,
                    "resource": "https://data-mcp-staging.boston.gov/mcp",
                    "authorization_servers": ["${OAUTH_ISSUER}"],
                    "jwt": {
                        "issuer": "${OAUTH_ISSUER}",
                        "jwks_uri": "${OAUTH_JWKS_URI}",
                        "audience": "${OAUTH_AUDIENCE}",
                    },
                    "oauth_proxy": {
                        "enabled": True,
                        "client_id": "${OAUTH_CLIENT_ID}",
                        "client_secret": "${OAUTH_CLIENT_SECRET}",
                    },
                }
            }
        )

    assert auth_config.enabled is True
    assert auth_config.authorization_servers == ["https://auth.example.gov"]
    assert auth_config.issuer == "https://auth.example.gov"
    assert auth_config.jwks_uri == "https://auth.example.gov/.well-known/jwks.json"
    assert auth_config.audience == "https://data-mcp-staging.boston.gov/mcp"
    assert auth_config.upstream_client_id == "upstream-client"
    assert auth_config.upstream_client_secret == "upstream-secret"


def test_build_protected_resource_metadata():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://auth.example.gov"],
        scopes_supported=["openid"],
        required_scopes=[],
        issuer="https://auth.example.gov",
        jwks_uri="https://auth.example.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
    )

    metadata = build_protected_resource_metadata(auth_config)

    assert metadata == {
        "resource": "https://data-mcp-staging.boston.gov/mcp",
        "authorization_servers": ["https://auth.example.gov"],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid"],
    }


def test_build_www_authenticate_header_uses_path_specific_metadata():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://auth.example.gov"],
        scopes_supported=[],
        required_scopes=["openid"],
        issuer="https://auth.example.gov",
        jwks_uri="https://auth.example.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
    )

    header = build_www_authenticate_header(auth_config)

    assert header.startswith("Bearer ")
    assert (
        'resource_metadata="https://data-mcp-staging.boston.gov'
        '/.well-known/oauth-protected-resource/mcp"'
        in header
    )
    assert 'scope="openid"' in header


def _jwt_fixture(scope: str = "openid profile"):
    _JWKS_CACHE.clear()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "test-key"
    token = jwt.encode(
        {
            "iss": "https://auth.example.gov",
            "aud": "https://data-mcp-staging.boston.gov/mcp",
            "exp": int(time.time()) + 300,
            "scope": scope,
            "sub": "user-123",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    return token, {"keys": [jwk]}


@pytest.mark.asyncio
async def test_validate_bearer_token_uses_jwks():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://auth.example.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://auth.example.gov",
        jwks_uri="https://auth.example.gov/.well-known/jwks.json",
        audience="https://data-mcp-staging.boston.gov/mcp",
        algorithms=["RS256"],
    )
    token, jwks = _jwt_fixture()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = jwks

    with patch("server.auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = response
        mock_client_class.return_value = mock_client

        result = await validate_bearer_token(token, auth_config)

    assert result.valid is True
    mock_client.get.assert_awaited_once()
    assert mock_client.get.call_args.args[0] == "https://auth.example.gov/.well-known/jwks.json"


@pytest.mark.asyncio
async def test_validate_bearer_token_rejects_missing_scope():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://auth.example.gov"],
        scopes_supported=["openid"],
        required_scopes=["admin"],
        issuer="https://auth.example.gov",
        jwks_uri="https://auth.example.gov/.well-known/jwks.json",
        audience="https://data-mcp-staging.boston.gov/mcp",
        algorithms=["RS256"],
    )
    token, jwks = _jwt_fixture(scope="openid")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = jwks

    with patch("server.auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = response
        mock_client_class.return_value = mock_client

        result = await validate_bearer_token(token, auth_config)

    assert result.valid is False
    assert result.error == "insufficient_scope"


@pytest.mark.asyncio
async def test_validate_opaque_bearer_token_via_userinfo():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://home.boston.gov/",
        jwks_uri="https://home.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        userinfo_endpoint="https://home.boston.gov/userinfo",
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"sub": "user-123"}

    with patch("server.auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = response
        mock_client_class.return_value = mock_client

        result = await validate_bearer_token("opaque-access-token", auth_config)

    assert result.valid is True
    mock_client.get.assert_awaited_once()
    assert mock_client.get.call_args.args[0] == "https://home.boston.gov/userinfo"
    assert (
        mock_client.get.call_args.kwargs["headers"]["Authorization"]
        == "Bearer opaque-access-token"
    )


@pytest.mark.asyncio
async def test_validate_opaque_bearer_token_rejects_userinfo_failure():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://home.boston.gov/",
        jwks_uri="https://home.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        userinfo_endpoint="https://home.boston.gov/userinfo",
    )
    response = MagicMock()
    response.status_code = 401

    with patch("server.auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = response
        mock_client_class.return_value = mock_client

        result = await validate_bearer_token("opaque-access-token", auth_config)

    assert result.valid is False
    assert result.error == "invalid_token"


def test_build_authorization_server_metadata_uses_proxy_issuer():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
    )

    metadata = build_authorization_server_metadata(auth_config)

    assert metadata["issuer"] == "https://data-mcp-staging.boston.gov"
    assert (
        metadata["authorization_endpoint"]
        == "https://data-mcp-staging.boston.gov/oauth2/auth"
    )
    assert (
        metadata["registration_endpoint"]
        == "https://data-mcp-staging.boston.gov/oauth2/register"
    )
    assert (
        metadata["jwks_uri"]
        == "https://data-mcp-staging.boston.gov/.well-known/jwks.json"
    )
    assert metadata["scopes_supported"] == ["openid"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]


def test_dynamic_client_registration_returns_public_client():
    from server.auth import build_dynamic_client_registration_response

    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://home.boston.gov/",
        jwks_uri="https://home.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_client_id="72d91a24ba0c4dff9dd3e3a0ac80f0da",
    )

    registration = build_dynamic_client_registration_response(
        auth_config,
        json.dumps(
            {
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "token_endpoint_auth_method": "none",
            }
        ),
    )

    assert registration["client_id"] == "72d91a24ba0c4dff9dd3e3a0ac80f0da"
    assert registration["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in registration
    assert registration["redirect_uris"] == [
        "https://claude.ai/api/mcp/auth_callback"
    ]


def test_dynamic_client_registration_accepts_localhost_cli_redirects():
    """mcp-remote and Gemini CLI register as public PKCE clients on localhost."""
    from server.auth import build_dynamic_client_registration_response

    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp.boston.gov/mcp",
        authorization_servers=["https://data-mcp.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://home.boston.gov/",
        jwks_uri="https://home.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_client_id="72d91a24ba0c4dff9dd3e3a0ac80f0da",
    )

    registration = build_dynamic_client_registration_response(
        auth_config,
        json.dumps(
            {
                "client_name": "mcp-remote",
                "redirect_uris": [
                    "http://localhost:3334/oauth/callback",
                    "http://127.0.0.1:3334/oauth/callback",
                ],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
            }
        ),
    )

    assert registration["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in registration
    assert registration["redirect_uris"] == [
        "http://localhost:3334/oauth/callback",
        "http://127.0.0.1:3334/oauth/callback",
    ]


def test_build_callback_redirect_restores_localhost_cli_redirect():
    """After Strivacity login, the proxy must send the code back to the CLI."""
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp.boston.gov/mcp",
        authorization_servers=["https://data-mcp.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp.boston.gov/oauth2/callback",
    )
    redirect, state_cookie = build_authorization_redirect(
        auth_config,
        (
            "client_id=mcp-remote&scope=openid&"
            "redirect_uri=http%3A%2F%2Flocalhost%3A3334%2Foauth%2Fcallback"
            "&state=cli-state&code_challenge=challenge"
            "&code_challenge_method=S256"
        ),
    )
    from urllib.parse import parse_qsl, urlparse

    assert (
        "redirect_uri=https%3A%2F%2Fdata-mcp.boston.gov%2Foauth2%2Fcallback"
        in redirect
    )
    idp_state = dict(parse_qsl(urlparse(redirect).query))["state"]
    callback, _expire_cookie = build_callback_redirect(
        f"code=test-code&state={idp_state}",
        {"cookie": state_cookie.split(";", 1)[0]},
    )

    assert callback.startswith("http://localhost:3334/oauth/callback?")
    assert "code=test-code" in callback
    assert "state=cli-state" in callback


def test_build_authorization_redirect_filters_scope_and_resource():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
    )

    redirect, state_cookie = build_authorization_redirect(
        auth_config,
        (
            "client_id=abc&scope=openid+offline_access&"
            "resource=https%3A%2F%2Fdata-mcp-staging.boston.gov%2Fmcp"
        ),
    )

    assert redirect.startswith("https://strivacity-test.boston.gov/oauth2/auth?")
    assert "response_mode=query" in redirect
    assert state_cookie is None
    assert "scope=openid" in redirect
    assert "offline_access" not in redirect
    assert "resource=" not in redirect


def test_build_authorization_redirect_wraps_callback_state():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )

    redirect, state_cookie = build_authorization_redirect(
        auth_config,
        (
            "client_id=abc&scope=openid&"
            "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            "%3Fsession_id%3Dsession-123%26short_app_id%3Dapp&state=claude-state&"
            "code_challenge=challenge&code_challenge_method=S256"
        ),
    )

    assert (
        "redirect_uri=https%3A%2F%2Fdata-mcp-staging.boston.gov%2Foauth2%2Fcallback"
        in redirect
    )
    assert "state=claude-state" not in redirect
    assert "state=" in redirect
    assert "code_challenge=challenge" in redirect
    assert "code_challenge_method=S256" in redirect
    assert "response_type=code" in redirect
    assert "response_mode=query" in redirect
    assert state_cookie is not None
    assert "opencontext_oauth_state=" in state_cookie


def test_build_callback_redirect_restores_claude_state():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    redirect, state_cookie = build_authorization_redirect(
        auth_config,
        (
            "client_id=abc&scope=openid&"
            "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            "%3Fsession_id%3Dsession-123%26short_app_id%3Dapp&state=claude-state"
        ),
    )
    from urllib.parse import parse_qsl, urlparse

    idp_state = dict(parse_qsl(urlparse(redirect).query))["state"]
    callback, expire_cookie = build_callback_redirect(
        f"code=test-code&iss=https%3A%2F%2Fstrivacity-test.boston.gov%2F&state={idp_state}",
        {"cookie": state_cookie.split(";", 1)[0]},
    )

    assert callback.startswith("https://claude.ai/api/mcp/auth_callback?")
    assert "session_id=session-123" in callback
    assert "short_app_id=app" in callback
    assert "code=test-code" in callback
    assert "state=claude-state" in callback
    assert "iss=" not in callback
    assert "Max-Age=0" in expire_cookie


def test_build_callback_redirect_recovers_state_from_cookie():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    _, state_cookie = build_authorization_redirect(
        auth_config,
        (
            "client_id=abc&scope=openid&"
            "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            "%3Fsession_id%3Dsession-123%26short_app_id%3Dapp&state=claude-state"
        ),
    )
    cookie_pair = state_cookie.split(";", 1)[0]

    callback, _ = build_callback_redirect(
        "code=test-code",
        {"cookie": cookie_pair},
    )

    assert "session_id=session-123" in callback
    assert "code=test-code" in callback
    assert "state=claude-state" in callback


def test_build_callback_redirect_synthesizes_state_when_claude_omits_it():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    _, state_cookie = build_authorization_redirect(
        auth_config,
        (
            "client_id=abc&scope=openid&"
            "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            "%3Fsession_id%3Dsession-123%26short_app_id%3Dapp"
        ),
    )
    cookie_pair = state_cookie.split(";", 1)[0]

    callback, _ = build_callback_redirect(
        "code=test-code",
        {"cookie": cookie_pair},
    )

    assert "session_id=session-123" in callback
    assert "code=test-code" in callback
    assert "state=" in callback


def test_build_callback_redirect_forwards_raw_claude_callback_params():
    callback, expire_cookie = build_callback_redirect(
        (
            "session_id=session-123&short_app_id=app&language=en-US&"
            "code=test-code&state=raw-claude-state&"
            "iss=https%3A%2F%2Fstrivacity-test.boston.gov%2F"
        )
    )

    assert callback.startswith("https://claude.ai/api/mcp/auth_callback?")
    assert "session_id=session-123" in callback
    assert "short_app_id=app" in callback
    assert "language=en-US" in callback
    assert "code=test-code" in callback
    assert "state=raw-claude-state" in callback
    assert "iss=" not in callback
    assert "Max-Age=0" in expire_cookie


def test_fragment_recovery_is_needed_for_empty_callback():
    assert callback_needs_fragment_recovery(
        "session_id=session-123&short_app_id=app&language=en-US"
    )
    assert not callback_needs_fragment_recovery("code=test-code&state=state-123")
    page = build_fragment_recovery_page()
    assert "window.location.hash" in page
    assert "setTimeout(tryComplete, 250)" in page


def test_has_oauth_authorize_params():
    from server.auth import has_oauth_authorize_params

    full = (
        "response_type=code&client_id=abc&"
        "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&"
        "state=state&code_challenge=challenge&code_challenge_method=S256"
    )
    assert has_oauth_authorize_params(full)
    assert not has_oauth_authorize_params(
        "session_id=session-123&short_app_id=app&language=en-US"
    )


def test_rebuild_pending_authorize_url_from_cookie_fields():
    from server.auth import (
        _encode_proxy_state,
        rebuild_pending_authorize_url,
    )

    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    cookie_value = _encode_proxy_state(
        "https://claude.ai/api/mcp/auth_callback?session_id=session-123",
        "claude-state",
        oauth_pending={
            "client_id": "37be6db89eba4a4683857303f209213e",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        },
    )
    decoded = __import__("server.auth", fromlist=["_decode_proxy_state"])._decode_proxy_state(
        cookie_value
    )
    url = rebuild_pending_authorize_url(auth_config, decoded)
    assert url is not None
    assert url.startswith("https://strivacity-test.boston.gov/oauth2/auth?")
    assert "client_id=37be6db89eba4a4683857303f209213e" in url
    assert "code_challenge=challenge" in url
    assert (
        "redirect_uri=https%3A%2F%2Fdata-mcp-staging.boston.gov%2Foauth2%2Fcallback"
        in url
    )


def test_claude_connector_help_page_shows_mcp_url():
    from server.auth import build_claude_connector_help_page

    page = build_claude_connector_help_page(
        "https://data-mcp-staging.boston.gov/mcp",
        "/oauth2/callback",
    )
    assert "https://data-mcp-staging.boston.gov/mcp" in page
    assert "/oauth2/callback" in page


def test_authorize_is_claude_bootstrap_only():
    from server.auth import authorize_is_claude_bootstrap_only

    assert authorize_is_claude_bootstrap_only(
        "session_id=session-123&short_app_id=app&language=en-US&opencontext_from_callback=1"
    )
    assert not authorize_is_claude_bootstrap_only(
        "client_id=abc&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        "&state=state&code_challenge=challenge&code_challenge_method=S256"
    )
    from server.auth import (
        build_callback_diagnostic_error,
        callback_has_oauth_response,
        callback_is_claude_session_bootstrap,
        callback_is_claude_session_only,
        classify_oauth_callback,
    )

    query = "session_id=session-123&short_app_id=app&language=en-US"
    assert callback_is_claude_session_bootstrap(query)
    assert callback_is_claude_session_only(query)
    assert not callback_has_oauth_response(query)
    assert classify_oauth_callback(query) == "claude_session_bootstrap"
    assert not callback_is_claude_session_only("code=test-code&state=state-123")
    assert callback_has_oauth_response("error=access_denied&state=state-123")
    assert classify_oauth_callback("error=access_denied&state=state-123") == "idp_error"

    diagnostic = build_callback_diagnostic_error(
        query,
        "/oauth2/callback",
        "GET",
        {"referer": "https://claude.ai/", "user-agent": "test-agent"},
        "test reason",
    )
    assert diagnostic["callback_kind"] == "claude_session_bootstrap"
    assert diagnostic["query_params"]["session_id"] == "session-123"
    assert "code" not in diagnostic["query_params"]


@pytest.mark.asyncio
async def test_proxy_token_request_forwards_pkce_and_strips_caller_secret():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.text = '{"access_token":"redacted"}'

    with patch("server.auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = response
        mock_client_class.return_value = mock_client

        await proxy_token_request(
            auth_config,
            (
                "grant_type=authorization_code&code=test-code&code_verifier=pkce&"
                "client_secret=caller-secret&"
                "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&"
                "scope=openid+offline_access&resource=https%3A%2F%2Fdata-mcp-staging.boston.gov%2Fmcp"
            ),
            {"authorization": "Basic abc123"},
        )

    forwarded_form = mock_client.post.call_args.kwargs["data"]
    forwarded_headers = mock_client.post.call_args.kwargs["headers"]
    assert forwarded_form["code_verifier"] == "pkce"
    assert "client_secret" not in forwarded_form
    assert "resource" not in forwarded_form
    assert forwarded_form["scope"] == "openid"
    assert (
        forwarded_form["redirect_uri"]
        == "https://data-mcp-staging.boston.gov/oauth2/callback"
    )
    assert "Authorization" not in forwarded_headers


@pytest.mark.asyncio
async def test_proxy_token_request_injects_upstream_client_credentials():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
        upstream_client_id="7dd7654fc96c4fe5a18e91daaaf54b0b",
        upstream_client_secret="upstream-secret",
    )
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.text = '{"access_token":"redacted"}'

    with patch("server.auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = response
        mock_client_class.return_value = mock_client

        await proxy_token_request(
            auth_config,
            (
                "grant_type=authorization_code&code=test-code&code_verifier=pkce&"
                "client_id=claude-client&client_secret=caller-secret&"
                "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
            ),
            {},
        )

    forwarded_form = mock_client.post.call_args.kwargs["data"]
    forwarded_headers = mock_client.post.call_args.kwargs["headers"]
    assert forwarded_form["client_id"] == "7dd7654fc96c4fe5a18e91daaaf54b0b"
    assert "client_secret" not in forwarded_form
    assert forwarded_form["code_verifier"] == "pkce"
    assert (
        forwarded_form["redirect_uri"]
        == "https://data-mcp-staging.boston.gov/oauth2/callback"
    )
    expected_basic = base64.b64encode(
        b"7dd7654fc96c4fe5a18e91daaaf54b0b:upstream-secret"
    ).decode("ascii")
    assert forwarded_headers["Authorization"] == f"Basic {expected_basic}"


def test_build_authorization_redirect_uses_upstream_client_id():
    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
        upstream_client_id="7dd7654fc96c4fe5a18e91daaaf54b0b",
        upstream_client_secret="upstream-secret",
    )

    redirect, _ = build_authorization_redirect(
        auth_config,
        (
            "response_type=code&client_id=claude-client&scope=openid&"
            "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&"
            "state=claude-state&code_challenge=challenge&code_challenge_method=S256"
        ),
    )

    assert "client_id=7dd7654fc96c4fe5a18e91daaaf54b0b" in redirect
    assert "client_id=claude-client" not in redirect
    assert (
        "redirect_uri=https%3A%2F%2Fdata-mcp-staging.boston.gov%2Foauth2%2Fcallback"
        in redirect
    )


@pytest.mark.asyncio
async def test_proxy_token_request_extracts_client_id_from_basic_auth():
    import base64

    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.text = '{"access_token":"redacted"}'
    basic = base64.b64encode(b"37be6db89eba4a4683857303f209213e:secret").decode("ascii")

    with patch("server.auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = response
        mock_client_class.return_value = mock_client

        await proxy_token_request(
            auth_config,
            (
                "grant_type=authorization_code&code=test-code&code_verifier=pkce&"
                "redirect_uri=https%3A%2F%2Fdata-mcp-staging.boston.gov%2Foauth2%2Fcallback"
            ),
            {"authorization": f"Basic {basic}"},
        )

    forwarded_form = mock_client.post.call_args.kwargs["data"]
    assert forwarded_form["client_id"] == "37be6db89eba4a4683857303f209213e"
    assert "client_secret" not in forwarded_form


def test_build_callback_redirect_recovers_pending_state_without_cookie():
    from server.auth import (
        _decode_proxy_state,
        _encode_proxy_state,
        _idp_state_from_decoded,
    )
    from server.oauth_pending_store import save_pending_proxy_state

    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    proxy_state = _encode_proxy_state(
        "https://claude.ai/api/mcp/auth_callback?session_id=session-123",
        "claude-state",
        oauth_pending={
            "client_id": "abc",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        },
        idp_state="idp-state-token",
    )
    save_pending_proxy_state("state:idp-state-token", proxy_state)

    callback, _ = build_callback_redirect(
        "code=test-code&state=idp-state-token",
        {},
    )

    assert "session_id=session-123" in callback
    assert "state=claude-state" in callback
    assert "code=test-code" in callback


def test_pending_authorize_url_from_session_store_without_cookie():
    from server.auth import (
        build_authorization_redirect,
        get_valid_pending_authorize_url,
    )

    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    query = (
        "client_id=abc&scope=openid&"
        "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        "%3Fsession_id%3Dsession-abc%26short_app_id%3Dapp&state=claude-state&"
        "code_challenge=challenge&code_challenge_method=S256"
    )
    build_authorization_redirect(auth_config, query)
    pending_url = get_valid_pending_authorize_url(
        auth_config, None, "session-abc"
    )
    assert pending_url is not None
    assert pending_url.startswith("https://strivacity-test.boston.gov/oauth2/auth?")
    assert "code_challenge=challenge" in pending_url
    from server.auth import build_oauth_continue_url

    auth_config = AuthConfig(
        enabled=True,
        resource="https://data-mcp-staging.boston.gov/mcp",
        authorization_servers=["https://data-mcp-staging.boston.gov"],
        scopes_supported=["openid"],
        required_scopes=["openid"],
        issuer="https://strivacity-test.boston.gov/",
        jwks_uri="https://strivacity-test.boston.gov/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        oauth_proxy_enabled=True,
        upstream_authorization_endpoint="https://strivacity-test.boston.gov/oauth2/auth",
        upstream_token_endpoint="https://strivacity-test.boston.gov/oauth2/token",
        callback_url="https://data-mcp-staging.boston.gov/oauth2/callback",
    )
    assert (
        build_oauth_continue_url(auth_config)
        == "https://data-mcp-staging.boston.gov/oauth2/continue"
    )


def test_authorization_waiting_page_uses_sign_in_url():
    from server.auth import build_authorization_waiting_page

    strivacity = (
        "https://strivacity-test.boston.gov/oauth2/auth?"
        "client_id=abc&code_challenge=challenge"
    )
    page = build_authorization_waiting_page(strivacity)
    assert strivacity in page
    assert "window.location.replace" not in page
    assert "target=\"_top\"" in page
    assert "window.top.location.assign" in page

    auto = build_authorization_waiting_page(strivacity, auto_redirect=True)
    assert "window.top.location.replace" in auto


def test_no_pending_auth_page():
    from server.auth import build_no_pending_auth_page

    page = build_no_pending_auth_page("https://data-mcp-staging.boston.gov/mcp")
    assert "No pending authorization request" in page
    assert "https://data-mcp-staging.boston.gov/mcp" in page


def test_claude_session_callback_error_page():
    from server.auth import build_claude_session_callback_error_page

    page = build_claude_session_callback_error_page(
        "https://data-mcp-staging.boston.gov/mcp"
    )
    assert "wrong callback" in page.lower()
    assert "https://data-mcp-staging.boston.gov/mcp" in page
    assert "/oauth2/continue" not in page
