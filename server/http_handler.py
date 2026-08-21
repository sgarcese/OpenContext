"""Universal HTTP handler for OpenContext MCP server.

This handler provides cloud-agnostic HTTP request processing that can be
used by any cloud provider adapter (AWS Lambda, GCP Cloud Functions, Azure Functions, etc.).
"""

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

from core.logging_utils import (
    configure_json_logging,
    format_http_exchange_log,
    format_request_log,
    format_response_log,
)
from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager
from core.validators import (
    ConfigurationError,
    get_logging_config,
    load_and_validate_config,
)
from server.auth import (
    authorize_is_claude_bootstrap_only,
    build_authorization_redirect,
    build_claude_connector_help_page,
    build_no_pending_auth_page,
    build_callback_diagnostic_error,
    build_callback_redirect,
    build_authorization_server_metadata,
    build_dynamic_client_registration_response,
    build_fragment_recovery_page,
    callback_is_claude_session_bootstrap,
    callback_is_claude_session_only,
    callback_needs_fragment_recovery,
    classify_oauth_callback,
    clear_authorization_pending,
    get_valid_pending_authorize_url,
    has_oauth_authorize_params,
    auth_is_enabled,
    build_protected_resource_metadata,
    build_www_authenticate_header,
    extract_bearer_token,
    get_auth_config,
    is_authorization_server_metadata_path,
    is_jwks_path,
    is_oauth_authorize_path,
    is_oauth_callback_path,
    is_oauth_continue_path,
    is_oauth_register_path,
    is_oauth_token_path,
    is_protected_resource_metadata_path,
    OAUTH_STATE_COOKIE_NAME,
    proxy_jwks_request,
    proxy_token_request,
    validate_bearer_token,
)

# Module-level default: configure JSON logging so imports are side-effect-free.
# Log level may be refined once configuration is loaded at runtime.
configure_json_logging(level="INFO", pretty=False)
logger = logging.getLogger(__name__)


def _safe_oauth_redirect_log(
    path: str,
    status_code: int,
    location: Optional[str],
    headers: Dict[str, str],
    query_string: str,
    sets_state_cookie: bool = False,
) -> Dict[str, Any]:
    cookie_header = headers.get("cookie") or headers.get("Cookie") or ""
    summary: Dict[str, Any] = {
        "oauth_path": path,
        "status_code": status_code,
        "cookie_header_present": bool(cookie_header),
        "has_state_cookie": OAUTH_STATE_COOKIE_NAME in cookie_header,
        "sets_state_cookie": sets_state_cookie,
        **_safe_oauth_query_summary(query_string),
    }
    if location:
        summary.update(_safe_redirect_summary(location))
        summary["location"] = location
    return summary


def _safe_oauth_query_summary(query_string: str) -> Dict[str, Any]:
    params = dict(parse_qsl(query_string, keep_blank_values=True))
    redirect_uri = params.get("redirect_uri", "")
    parsed_redirect = urlparse(redirect_uri) if redirect_uri else None
    return {
        "query_keys": sorted(params.keys()),
        "has_code": bool(params.get("code")),
        "has_error": bool(params.get("error")),
        "has_state": bool(params.get("state")),
        "state_length": len(params.get("state", "")),
        "has_redirect_uri": bool(redirect_uri),
        "redirect_uri_host": parsed_redirect.netloc if parsed_redirect else None,
        "redirect_uri_path": parsed_redirect.path if parsed_redirect else None,
        "redirect_query_keys": (
            sorted(dict(parse_qsl(parsed_redirect.query)).keys())
            if parsed_redirect and parsed_redirect.query
            else []
        ),
        "scope": params.get("scope"),
    }


def _safe_redirect_summary(redirect_url: str) -> Dict[str, Any]:
    parsed = urlparse(redirect_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return {
        "redirect_host": parsed.netloc,
        "redirect_path": parsed.path,
        "redirect_query_keys": sorted(params.keys()),
        "redirect_has_code": bool(params.get("code")),
        "redirect_has_state": bool(params.get("state")),
        "redirect_state_length": len(params.get("state", "")),
    }


def _safe_json_error_summary(body: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(body)
    except Exception:
        return {"upstream_error": None, "upstream_error_description": None}
    if not isinstance(parsed, dict):
        return {"upstream_error": None, "upstream_error_description": None}
    return {
        "upstream_error": parsed.get("error"),
        "upstream_error_description": parsed.get("error_description"),
    }


# Global variables for container reuse (warm starts)
_plugin_manager: Optional[PluginManager] = None
_mcp_server: Optional[MCPServer] = None
_config: Optional[Dict[str, Any]] = None


def _configure_logging_from_config(config: Dict[str, Any]) -> None:
    """Re-configure JSON logging using the loaded configuration dictionary."""
    logging_config = get_logging_config(config)
    log_level = logging_config.get("level", "INFO")
    configure_json_logging(level=log_level, pretty=False)


def _load_config() -> Dict[str, Any]:
    """Load configuration from environment or embedded config.

    Returns:
        Configuration dictionary
    """
    global _config

    if _config is not None:
        return _config

    # Try to load from environment variable (set by Terraform)
    config_json = os.environ.get("OPENCONTEXT_CONFIG")
    if config_json:
        try:
            _config = json.loads(config_json)
            logger.info("Loaded configuration from environment variable")
            return _config
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse config from environment: {e}")
            raise

    # Fall back to loading from config.yaml (for local testing)
    try:
        _config = load_and_validate_config("config.yaml")
        logger.info("Loaded configuration from config.yaml")
        return _config
    except FileNotFoundError:
        logger.error(
            "No configuration found. Set OPENCONTEXT_CONFIG environment variable "
            "or ensure config.yaml exists."
        )
        raise


async def _initialize_server() -> None:
    """Initialize plugin manager and MCP server.

    This function is called on first request (cold start) and reuses
    the initialized instances for subsequent requests (warm starts).
    """
    global _plugin_manager, _mcp_server

    if _plugin_manager is not None and _mcp_server is not None:
        return

    try:
        config = _load_config()

        # Configure logging now that we have real configuration
        _configure_logging_from_config(config)

        # Initialize Plugin Manager
        _plugin_manager = PluginManager(config)

        # Load plugins (validates ONE plugin enabled)
        await _plugin_manager.load_plugins()

        # Initialize MCP Server
        _mcp_server = MCPServer(_plugin_manager)

        logger.info("OpenContext MCP server initialized successfully")

    except ConfigurationError as e:
        # Log error and crash
        logger.error(f"Configuration error: {e}")
        raise RuntimeError(f"Configuration error: {e}") from e
    except Exception as e:
        logger.error(f"Failed to initialize server: {e}", exc_info=True)
        raise


def _wants_html_response(headers: Dict[str, str]) -> bool:
    accept = headers.get("accept", "")
    user_agent = headers.get("user-agent", "")
    return "text/html" in accept or "Mozilla" in user_agent or "Chrome" in user_agent


class UniversalHTTPHandler:
    """Universal HTTP handler for cloud-agnostic request processing."""

    def __init__(self) -> None:
        """Initialize the universal HTTP handler."""
        logger.info("UniversalHTTPHandler initialized")

    @staticmethod
    def _get_cors_headers() -> Dict[str, str]:
        """Get standard CORS headers for responses.

        Returns:
            Dictionary of CORS headers
        """
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "authorization, content-type",
            "Access-Control-Expose-Headers": (
                "www-authenticate, x-request-id, mcp-session-id"
            ),
        }

    def _json_response(
        self,
        status_code: int,
        body: Dict[str, Any],
        request_id: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], str]:
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        headers.update(self._get_cors_headers())
        if extra_headers:
            headers.update(extra_headers)
        return (status_code, headers, json.dumps(body))

    def _log_incoming_request(
        self,
        request_id: str,
        method: str,
        path: str,
        query_string: str,
        headers: Dict[str, str],
        body: str,
    ) -> None:
        logger.info(
            "HTTP request received",
            extra=format_http_exchange_log(
                request_id=request_id,
                http_method=method,
                request_path=path,
                query_string=query_string,
                request_headers=headers,
                request_body=body,
                phase="request",
            ),
        )

    def _log_outgoing_response(
        self,
        request_id: str,
        method: str,
        path: str,
        query_string: str,
        headers: Dict[str, str],
        body: str,
        status_code: int,
        response_headers: Dict[str, str],
        response_body: str,
        start_time: float,
        oauth_event: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "HTTP response sent",
            extra=format_http_exchange_log(
                request_id=request_id,
                http_method=method,
                request_path=path,
                query_string=query_string,
                request_headers=headers,
                request_body=body,
                phase="response",
                status_code=status_code,
                response_headers=response_headers,
                response_body=response_body,
                duration_ms=duration_ms,
                oauth_event=oauth_event,
                extra=extra,
            ),
        )

    async def handle_request(
        self,
        method: str,
        path: str,
        body: str,
        headers: Dict[str, str],
        query_string: str = "",
        request_id: Optional[str] = None,
    ) -> Tuple[int, Dict[str, str], str]:
        """Handle a universal HTTP request."""
        start_time = time.perf_counter()
        request_id = request_id or "unknown"
        self._log_incoming_request(
            request_id, method, path, query_string, headers, body
        )
        status_code, response_headers, response_body = await self._handle_request_impl(
            method=method,
            path=path,
            body=body,
            headers=headers,
            query_string=query_string,
            request_id=request_id,
            start_time=start_time,
        )
        self._log_outgoing_response(
            request_id,
            method,
            path,
            query_string,
            headers,
            body,
            status_code,
            response_headers,
            response_body,
            start_time,
        )
        return status_code, response_headers, response_body

    async def _handle_request_impl(
        self,
        method: str,
        path: str,
        body: str,
        headers: Dict[str, str],
        query_string: str = "",
        request_id: Optional[str] = None,
        start_time: Optional[float] = None,
    ) -> Tuple[int, Dict[str, str], str]:
        """Process HTTP request after request-level logging."""
        start_time = start_time or time.perf_counter()
        request_id = request_id or "unknown"

        if (
            is_authorization_server_metadata_path(path)
            or is_jwks_path(path)
            or is_oauth_authorize_path(path)
            or is_oauth_callback_path(path)
            or is_oauth_continue_path(path)
            or is_oauth_register_path(path)
            or is_oauth_token_path(path)
        ):
            try:
                config = _load_config()
                auth_config = get_auth_config(config)
            except Exception as e:
                logger.error(
                    "Failed to load OAuth proxy configuration: %s",
                    e,
                    extra={"request_id": request_id},
                    exc_info=True,
                )
                return self._json_response(
                    500,
                    {
                        "error": "server_error",
                        "error_description": "OAuth proxy configuration failed",
                    },
                    request_id,
                )

            if not auth_config.oauth_proxy_enabled:
                return self._json_response(
                    404,
                    {
                        "error": "not_found",
                        "error_description": "OAuth proxy is not enabled",
                    },
                    request_id,
                )

            if is_authorization_server_metadata_path(path):
                return self._json_response(
                    200,
                    build_authorization_server_metadata(auth_config),
                    request_id,
                )

            if is_oauth_register_path(path):
                if method != "POST":
                    return self._json_response(
                        405,
                        {
                            "error": "method_not_allowed",
                            "error_description": "Expected POST",
                        },
                        request_id,
                        {"Allow": "POST"},
                    )
                try:
                    registration = build_dynamic_client_registration_response(
                        auth_config, body
                    )
                except ValueError as e:
                    logger.warning(
                        "OAuth dynamic client registration rejected: %s",
                        e,
                        extra={"request_id": request_id},
                    )
                    return self._json_response(
                        400,
                        {
                            "error": "invalid_client_metadata",
                            "error_description": str(e),
                        },
                        request_id,
                    )
                logger.info(
                    "OAuth dynamic client registration",
                    extra={
                        "request_id": request_id,
                        "client_id": registration.get("client_id"),
                        "redirect_uri_count": len(
                            registration.get("redirect_uris") or []
                        ),
                        "token_endpoint_auth_method": registration.get(
                            "token_endpoint_auth_method"
                        ),
                    },
                )
                return self._json_response(201, registration, request_id)

            if is_jwks_path(path):
                if method != "GET":
                    return self._json_response(
                        405,
                        {
                            "error": "method_not_allowed",
                            "error_description": "Expected GET",
                        },
                        request_id,
                        {"Allow": "GET"},
                    )
                try:
                    status_code, response_headers, response_body = (
                        await proxy_jwks_request(auth_config)
                    )
                except Exception as e:
                    logger.warning(
                        "OAuth JWKS proxy failed: %s",
                        e,
                        extra={"request_id": request_id},
                        exc_info=True,
                    )
                    return self._json_response(
                        502,
                        {
                            "error": "temporarily_unavailable",
                            "error_description": "OAuth JWKS proxy failed",
                        },
                        request_id,
                    )
                response_headers.update(self._get_cors_headers())
                response_headers["X-Request-ID"] = request_id
                return (status_code, response_headers, response_body)

            if is_oauth_continue_path(path):
                if method != "GET":
                    return self._json_response(
                        405,
                        {
                            "error": "method_not_allowed",
                            "error_description": "Expected GET",
                        },
                        request_id,
                        {"Allow": "GET"},
                    )
                pending_url = get_valid_pending_authorize_url(
                    auth_config,
                    headers,
                    dict(parse_qsl(query_string, keep_blank_values=True)).get(
                        "session_id"
                    ),
                )
                cookie_header = headers.get("cookie") or headers.get("Cookie") or ""
                logger.info(
                    "OAuth continue request",
                    extra={
                        "request_id": request_id,
                        "has_valid_pending_authorize_url": bool(pending_url),
                        "has_state_cookie": OAUTH_STATE_COOKIE_NAME in cookie_header,
                    },
                )
                if not pending_url:
                    return self._json_response(
                        400,
                        {
                            "error": "invalid_request",
                            "error_description": "No pending authorization request",
                        },
                        request_id,
                    )
                logger.info(
                    "OAuth continue redirecting to upstream sign-in",
                    extra={
                        "request_id": request_id,
                        **_safe_redirect_summary(pending_url),
                    },
                )
                response_headers = {
                    "Location": pending_url,
                    "Cache-Control": "no-store",
                    "X-Request-ID": request_id,
                }
                response_headers.update(self._get_cors_headers())
                return (302, response_headers, "")

            if is_oauth_authorize_path(path):
                if method != "GET":
                    return self._json_response(
                        405,
                        {
                            "error": "method_not_allowed",
                            "error_description": "Expected GET",
                        },
                        request_id,
                        {"Allow": "GET"},
                    )
                logger.info(
                    "OAuth authorize proxy request",
                    extra={
                        "request_id": request_id,
                        **_safe_oauth_query_summary(query_string),
                    },
                )
                if has_oauth_authorize_params(query_string):
                    redirect_url, state_cookie = build_authorization_redirect(
                        auth_config, query_string
                    )
                    logger.info(
                        "OAuth authorize redirecting to upstream IdP",
                        extra={
                            "request_id": request_id,
                            "sets_state_cookie": bool(state_cookie),
                            **_safe_redirect_summary(redirect_url),
                            **_safe_oauth_query_summary(query_string),
                        },
                    )
                    # 302 to Strivacity with redirect_uri rewritten to our
                    # /oauth2/callback. Claude's inbound URL still shows its own
                    # redirect_uri; that is expected and is not what Strivacity sees.
                    response_headers = {
                        "Location": redirect_url,
                        "Cache-Control": "no-store",
                        "X-Request-ID": request_id,
                    }
                    if state_cookie:
                        response_headers["Set-Cookie"] = state_cookie
                    response_headers.update(self._get_cors_headers())
                    return (302, response_headers, "")

                if authorize_is_claude_bootstrap_only(query_string):
                    logger.warning(
                        "OAuth authorize bootstrap without OAuth params",
                        extra={
                            "request_id": request_id,
                            "callback_kind": classify_oauth_callback(query_string),
                            **_safe_oauth_query_summary(query_string),
                        },
                    )
                    if _wants_html_response(headers):
                        response_headers = {
                            "Content-Type": "text/html; charset=utf-8",
                            "Cache-Control": "no-store",
                            "X-Request-ID": request_id,
                        }
                        response_headers.update(self._get_cors_headers())
                        return (
                            200,
                            response_headers,
                            build_claude_connector_help_page(
                                auth_config.resource, path
                            ),
                        )
                    diagnostic = build_callback_diagnostic_error(
                        query_string,
                        path,
                        method,
                        headers,
                        (
                            "Claude session request arrived before OAuth authorize "
                            f"request. Use the MCP connector URL ({auth_config.resource}) "
                            "only."
                        ),
                    )
                    return self._json_response(400, diagnostic, request_id)

                diagnostic = build_callback_diagnostic_error(
                    query_string,
                    path,
                    method,
                    headers,
                    "Missing OAuth authorization parameters",
                )
                logger.warning(
                    "OAuth authorize missing parameters",
                    extra={"request_id": request_id, **diagnostic},
                )
                return self._json_response(400, diagnostic, request_id)

            if is_oauth_callback_path(path):
                if method != "GET":
                    return self._json_response(
                        405,
                        {
                            "error": "method_not_allowed",
                            "error_description": "Expected GET",
                        },
                        request_id,
                        {"Allow": "GET"},
                    )
                logger.info(
                    "OAuth callback proxy request",
                    extra={
                        "request_id": request_id,
                        "callback_kind": classify_oauth_callback(query_string),
                        **_safe_oauth_query_summary(query_string),
                        "has_state_cookie": OAUTH_STATE_COOKIE_NAME
                        in (headers.get("cookie") or headers.get("Cookie") or ""),
                        "referer": headers.get("referer"),
                        "user_agent": headers.get("user-agent"),
                    },
                )

                callback_params = dict(parse_qsl(query_string, keep_blank_values=True))
                if callback_params.get("error"):
                    try:
                        redirect_url, state_cookie = build_callback_redirect(
                            query_string, headers
                        )
                    except Exception as e:
                        logger.warning(
                            "OAuth callback IdP error, forward failed: %s",
                            e,
                            extra={
                                "request_id": request_id,
                                "idp_error": callback_params.get("error"),
                                "idp_error_description": callback_params.get(
                                    "error_description"
                                ),
                                **_safe_oauth_query_summary(query_string),
                            },
                            exc_info=True,
                        )
                        return self._json_response(
                            400,
                            {
                                "error": callback_params["error"],
                                "error_description": callback_params.get(
                                    "error_description"
                                ),
                                **build_callback_diagnostic_error(
                                    query_string,
                                    path,
                                    method,
                                    headers,
                                    "Identity provider returned an error",
                                ),
                            },
                            request_id,
                        )
                    logger.info(
                        "OAuth callback forwarding IdP error to Claude",
                        extra={
                            "request_id": request_id,
                            "idp_error": callback_params.get("error"),
                            **_safe_oauth_redirect_log(
                                path,
                                302,
                                redirect_url,
                                headers,
                                query_string,
                            ),
                        },
                    )
                    headers_out = {
                        "Location": redirect_url,
                        "Set-Cookie": state_cookie,
                    }
                    headers_out.update(self._get_cors_headers())
                    return (302, headers_out, "")

                if callback_params.get("code") and callback_params.get("state"):
                    try:
                        redirect_url, state_cookie = build_callback_redirect(
                            query_string, headers
                        )
                    except Exception as e:
                        logger.warning(
                            "OAuth callback proxy failed: %s",
                            e,
                            extra={
                                "request_id": request_id,
                                **_safe_oauth_query_summary(query_string),
                            },
                            exc_info=True,
                        )
                        return self._json_response(
                            400,
                            {
                                "error": "invalid_request",
                                "error_description": "Invalid OAuth callback state",
                            },
                            request_id,
                        )
                    logger.info(
                        "OAuth callback proxy redirect",
                        extra={
                            "request_id": request_id,
                            **_safe_oauth_redirect_log(
                                path,
                                302,
                                redirect_url,
                                headers,
                                query_string,
                            ),
                        },
                    )
                    headers_out = {
                        "Location": redirect_url,
                        "Set-Cookie": state_cookie,
                    }
                    headers_out.update(self._get_cors_headers())
                    clear_authorization_pending(
                        callback_params.get("session_id"),
                        callback_params.get("state"),
                    )
                    return (302, headers_out, "")

                if callback_params.get("code"):
                    diagnostic = build_callback_diagnostic_error(
                        query_string,
                        path,
                        method,
                        headers,
                        "Missing state in OAuth callback query string",
                    )
                    logger.warning(
                        "OAuth callback missing state",
                        extra={"request_id": request_id, **diagnostic},
                    )
                    return self._json_response(400, diagnostic, request_id)

                if callback_is_claude_session_bootstrap(query_string):
                    pending_url = get_valid_pending_authorize_url(
                        auth_config,
                        headers,
                        callback_params.get("session_id"),
                    )
                    cookie_header = headers.get("cookie") or headers.get("Cookie") or ""
                    logger.warning(
                        "OAuth callback Claude session bootstrap",
                        extra={
                            "request_id": request_id,
                            "session_id": callback_params.get("session_id"),
                            "has_valid_pending_authorize_url": bool(pending_url),
                            "has_state_cookie": OAUTH_STATE_COOKIE_NAME in cookie_header,
                            **_safe_redirect_summary(pending_url or ""),
                            **_safe_oauth_query_summary(query_string),
                        },
                    )
                    response_headers = {
                        "Content-Type": "text/html; charset=utf-8",
                        "Cache-Control": "no-store",
                        "X-Request-ID": request_id,
                    }
                    response_headers.update(self._get_cors_headers())
                    if pending_url:
                        response_headers.pop("Content-Type", None)
                        response_headers["Location"] = pending_url
                        return (302, response_headers, "")
                    body = build_no_pending_auth_page(auth_config.resource)
                    return (200, response_headers, body)

                if callback_is_claude_session_only(query_string):
                    pending_url = get_valid_pending_authorize_url(
                        auth_config,
                        headers,
                        callback_params.get("session_id"),
                    )
                    logger.warning(
                        "OAuth callback Claude session without code",
                        extra={
                            "request_id": request_id,
                            "has_valid_pending_authorize_url": bool(pending_url),
                            **_safe_redirect_summary(pending_url or ""),
                            **_safe_oauth_query_summary(query_string),
                        },
                    )
                    response_headers = {
                        "Content-Type": "text/html; charset=utf-8",
                        "Cache-Control": "no-store",
                        "X-Request-ID": request_id,
                    }
                    response_headers.update(self._get_cors_headers())
                    if pending_url:
                        response_headers.pop("Content-Type", None)
                        response_headers["Location"] = pending_url
                        return (302, response_headers, "")
                    body = build_no_pending_auth_page(auth_config.resource)
                    return (200, response_headers, body)

                if callback_needs_fragment_recovery(query_string):
                    logger.info(
                        "OAuth callback serving fragment recovery",
                        extra={"request_id": request_id},
                    )
                    response_headers = {
                        "Content-Type": "text/html; charset=utf-8",
                        "Cache-Control": "no-store",
                        "X-Request-ID": request_id,
                    }
                    response_headers.update(self._get_cors_headers())
                    return (200, response_headers, build_fragment_recovery_page())

                diagnostic = build_callback_diagnostic_error(
                    query_string,
                    path,
                    method,
                    headers,
                    "Missing authorization code",
                )
                logger.warning(
                    "OAuth callback invalid",
                    extra={"request_id": request_id, **diagnostic},
                )
                return self._json_response(400, diagnostic, request_id)

            if is_oauth_token_path(path):
                if method != "POST":
                    return self._json_response(
                        405,
                        {
                            "error": "method_not_allowed",
                            "error_description": "Expected POST",
                        },
                        request_id,
                        {"Allow": "POST"},
                    )
                try:
                    status_code, response_headers, response_body = (
                        await proxy_token_request(auth_config, body, headers)
                    )
                except Exception as e:
                    logger.warning(
                        "OAuth token proxy failed: %s",
                        e,
                        extra={"request_id": request_id},
                        exc_info=True,
                    )
                    return self._json_response(
                        502,
                        {
                            "error": "temporarily_unavailable",
                            "error_description": "OAuth token proxy failed",
                        },
                        request_id,
                    )
                logger.info(
                    "OAuth token proxy response",
                    extra={
                        "request_id": request_id,
                        "status_code": status_code,
                        **_safe_json_error_summary(response_body),
                    },
                )
                response_headers.update(self._get_cors_headers())
                response_headers["X-Request-ID"] = request_id
                return (status_code, response_headers, response_body)

        if is_protected_resource_metadata_path(path):
            if method != "GET":
                return self._json_response(
                    405,
                    {
                        "error": "method_not_allowed",
                        "error_description": "Expected GET",
                    },
                    request_id,
                    {"Allow": "GET"},
                )

            try:
                config = _load_config()
                auth_config = get_auth_config(config)
            except Exception as e:
                logger.error(
                    "Failed to load OAuth metadata configuration: %s",
                    e,
                    extra={"request_id": request_id},
                    exc_info=True,
                )
                return self._json_response(
                    500,
                    {
                        "error": "server_error",
                        "error_description": "OAuth metadata configuration failed",
                    },
                    request_id,
                )

            if not auth_config.enabled:
                return self._json_response(
                    404,
                    {
                        "error": "not_found",
                        "error_description": "OAuth is not enabled",
                    },
                    request_id,
                )

            return self._json_response(
                200,
                build_protected_resource_metadata(auth_config),
                request_id,
            )

        # Validate path - must be /mcp
        if path != "/mcp":
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32601,
                        "message": "Not Found",
                        "data": f"Path '{path}' not found. Expected '/mcp'",
                    },
                }
            )
            logger.warning(
                f"404 error: Path '{path}' not found",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "duration_ms": duration_ms,
                },
            )
            error_headers = {"Content-Type": "application/json"}
            error_headers.update(self._get_cors_headers())
            return (
                404,
                error_headers,
                error_body,
            )

        # Validate method - must be POST
        if method != "POST":
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32601,
                        "message": "Method Not Allowed",
                        "data": f"Method '{method}' not allowed. Expected 'POST'",
                    },
                }
            )
            logger.warning(
                f"405 error: Method '{method}' not allowed",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "duration_ms": duration_ms,
                },
            )
            error_headers = {"Content-Type": "application/json", "Allow": "POST"}
            error_headers.update(self._get_cors_headers())
            return (
                405,
                error_headers,
                error_body,
            )

        try:
            config = _load_config()
        except FileNotFoundError:
            # Local tests and development can exercise the HTTP handler without
            # a deployment config. The server initialization path still raises
            # later if a real MCP request needs config and none exists.
            config = {}

        if auth_is_enabled(config):
            auth_config = get_auth_config(config)
            token = extract_bearer_token(headers)
            authenticate_header = build_www_authenticate_header(auth_config)

            if token is None:
                return self._json_response(
                    401,
                    {
                        "error": "unauthorized",
                        "error_description": "Missing bearer token",
                    },
                    request_id,
                    {"www-authenticate": authenticate_header},
                )

            auth_result = await validate_bearer_token(token, auth_config)
            if not auth_result.valid:
                return self._json_response(
                    auth_result.status_code,
                    {
                        "error": auth_result.error,
                        "error_description": auth_result.description,
                    },
                    request_id,
                    {"www-authenticate": authenticate_header},
                )

        # Parse JSON to check if this is an initialize request
        # NOTE: This is intentionally parsing the JSON body separately from the
        # later parsing in _mcp_server.handle_http_request(). This early parsing
        # allows us to detect initialize requests and generate session IDs without
        # affecting error handling if the JSON is invalid. The body will be parsed
        # again later, which is an acceptable trade-off for error handling isolation.
        try:
            request_json = json.loads(body)
            is_initialize = request_json.get("method") == "initialize"
        except (json.JSONDecodeError, AttributeError):
            is_initialize = False

        # Generate session ID for initialize requests
        # NOTE: This session ID is for logging and tracing purposes only.
        # It is NOT implementing true session management - there is no persistent
        # session storage. The session ID is included in response headers to
        # help correlate logs and trace requests, but it does not maintain
        # any server-side session state.
        session_id = None
        if is_initialize:
            session_id = str(uuid.uuid4())
            logger.info(
                f"Initialize request detected, generating session ID: {session_id}",
                extra={"request_id": request_id},
            )

        # Log request details
        request_log_data = format_request_log(
            request_id=request_id,
            http_method=method,
            request_path=path,
            headers=headers,
            body=body,
            lambda_context=None,  # Not available in universal handler
        )
        logger.info("MCP request processing", extra=request_log_data)

        try:
            # Initialize server on first request
            await _initialize_server()

            # Handle request
            response = await _mcp_server.handle_http_request(body, headers)

            # Extract status code and body from response
            status_code = response.get("statusCode", 200)
            response_body = response.get("body", "")
            response_headers = response.get("headers", {}).copy()

            # Add session ID to response headers if this was an initialize request
            if session_id:
                response_headers["Mcp-Session-Id"] = session_id

            # Add request ID to response headers for tracing
            response_headers["X-Request-ID"] = request_id

            # Ensure Content-Type is set
            if "Content-Type" not in response_headers:
                response_headers["Content-Type"] = "application/json"

            # Add CORS headers
            response_headers.update(self._get_cors_headers())

            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Log response details
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=status_code,
                headers=response_headers,
                body=response_body,
                duration_ms=duration_ms,
                success=True,
            )
            logger.info("MCP request processed successfully", extra=response_log_data)

            return (status_code, response_headers, response_body)

        except ConfigurationError as e:
            # Configuration errors should crash
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Server configuration error",
                        "data": str(e),
                    },
                }
            )

            # Log error response
            error_headers = {"Content-Type": "application/json"}
            error_headers.update(self._get_cors_headers())
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=500,
                headers=error_headers,
                body=error_body,
                duration_ms=duration_ms,
                success=False,
            )
            logger.error(
                f"Configuration error in request {request_id}: {e}",
                extra={**response_log_data, "error_type": "ConfigurationError"},
                exc_info=True,
            )

            return (500, error_headers, error_body)

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": str(e),
                    },
                }
            )

            # Log error response
            error_headers = {"Content-Type": "application/json"}
            error_headers.update(self._get_cors_headers())
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=500,
                headers=error_headers,
                body=error_body,
                duration_ms=duration_ms,
                success=False,
            )
            logger.error(
                f"Error processing request {request_id}: {e}",
                extra={**response_log_data, "error_type": type(e).__name__},
                exc_info=True,
            )

            return (500, error_headers, error_body)

    def handle_options(
        self, request_id: Optional[str] = None
    ) -> Tuple[int, Dict[str, str], str]:
        """Handle CORS preflight OPTIONS request.

        Args:
            request_id: Optional request ID for logging/tracing

        Returns:
            Tuple of (status_code, response_headers, response_body)
        """
        request_id = request_id or "unknown"
        cors_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "authorization, content-type",
            "Access-Control-Expose-Headers": (
                "www-authenticate, x-request-id, mcp-session-id"
            ),
            "Access-Control-Max-Age": "86400",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }

        logger.info(
            "CORS preflight OPTIONS request handled",
            extra={"request_id": request_id},
        )

        return (200, cors_headers, "")
