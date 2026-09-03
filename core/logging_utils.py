"""Logging utilities for OpenContext.

Provides centralized JSON logging configuration and sensitive data sanitization.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from pythonjsonlogger import json as jsonlogger

# Sensitive keys to filter (case-insensitive)
SENSITIVE_KEYS = [
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth",
    "token",
    "bearer",
    "password",
    "passwd",
    "secret",
    "credential",
    "credentials",
    "access_token",
    "refresh_token",
    "session_id",
    "cookie",
]

# Sensitive header prefixes (case-insensitive)
SENSITIVE_HEADER_PREFIXES = [
    "x-api-key",
    "x-auth",
    "x-token",
    "x-secret",
    "authorization",
    "cookie",
]


def configure_json_logging(level: str = "INFO", pretty: bool = False) -> None:
    """Configure ALL loggers to use JSON format.

    This function sets up the root logger with JSON formatting, ensuring
    all child loggers inherit JSON format. Must be called before any
    other loggers are created.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        pretty: If True, use pretty-printed JSON (for local development).
                If False, use compact JSON (for CloudWatch).
    """
    # Get root logger
    root_logger = logging.getLogger()

    # Remove any existing handlers
    root_logger.handlers.clear()

    # Set log level
    log_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(log_level)

    # Create StreamHandler
    handler = logging.StreamHandler()
    handler.setLevel(log_level)

    if pretty:
        # Use pretty JSON formatter for local development
        formatter = _PrettyJsonFormatter()
    else:
        # Use compact JSON formatter for CloudWatch
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            timestamp=True,
        )

    handler.setFormatter(formatter)

    # Add handler to root logger
    root_logger.addHandler(handler)


class _PrettyJsonFormatter(logging.Formatter):
    """Pretty JSON formatter for local development.

    Formats logs as indented JSON for better readability in terminals.
    Also truncates very large nested structures to keep logs readable.
    """

    def __init__(self, max_string_length: int = 500, max_list_items: int = 10):
        """Initialize pretty formatter.

        Args:
            max_string_length: Maximum length for string values before truncation
            max_list_items: Maximum items in lists before truncation
        """
        super().__init__()
        self.max_string_length = max_string_length
        self.max_list_items = max_list_items

    def _truncate_value(self, value: Any, depth: int = 0) -> Any:
        """Recursively truncate large values for readability.

        Args:
            value: Value to truncate
            depth: Current nesting depth

        Returns:
            Truncated value
        """
        if depth > 3:  # Don't truncate beyond 3 levels deep
            return "..."

        if isinstance(value, str):
            if len(value) > self.max_string_length:
                return (
                    value[: self.max_string_length]
                    + f"... (truncated, {len(value)} chars)"
                )
            return value
        elif isinstance(value, dict):
            truncated = {}
            for k, v in list(value.items())[:20]:  # Limit dict keys
                truncated[k] = self._truncate_value(v, depth + 1)
            if len(value) > 20:
                truncated["..."] = f"(truncated, {len(value)} keys)"
            return truncated
        elif isinstance(value, list):
            truncated = [
                self._truncate_value(item, depth + 1)
                for item in value[: self.max_list_items]
            ]
            if len(value) > self.max_list_items:
                truncated.append(f"... (truncated, {len(value)} items)")
            return truncated
        else:
            return value

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as pretty JSON."""
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add any extra fields
        if hasattr(record, "__dict__"):
            for key, value in record.__dict__.items():
                if key not in (
                    "name",
                    "msg",
                    "args",
                    "created",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "module",
                    "msecs",
                    "message",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "thread",
                    "threadName",
                    "exc_info",
                    "exc_text",
                    "stack_info",
                    "asctime",
                    "datefmt",
                ):
                    # Truncate large values for readability
                    log_data[key] = self._truncate_value(value)

        # Format as pretty JSON
        try:
            return json.dumps(log_data, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            # Fallback to string representation if JSON serialization fails
            return json.dumps({"message": str(record.getMessage())}, indent=2)


def _is_sensitive_key(key: str) -> bool:
    """Check if a key is sensitive (case-insensitive).

    Args:
        key: Key to check

    Returns:
        True if key is sensitive
    """
    key_lower = key.lower()
    return any(sensitive_key in key_lower for sensitive_key in SENSITIVE_KEYS)


def sanitize_dict(data: Any, sensitive_keys: Optional[List[str]] = None) -> Any:
    """Recursively sanitize dictionary values for sensitive keys.

    Preserves structure but replaces sensitive values with [REDACTED].

    Args:
        data: Data to sanitize (dict, list, or primitive)
        sensitive_keys: Optional list of additional sensitive keys to check

    Returns:
        Sanitized data with same structure
    """
    if sensitive_keys is None:
        sensitive_keys = SENSITIVE_KEYS

    if isinstance(data, dict):
        sanitized = {}
        for key, value in data.items():
            # Check if key is sensitive
            if _is_sensitive_key(key) or (
                sensitive_keys
                and any(sk.lower() in key.lower() for sk in sensitive_keys)
            ):
                sanitized[key] = "[REDACTED]"
            else:
                # Recursively sanitize nested structures
                sanitized[key] = sanitize_dict(value, sensitive_keys)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_dict(item, sensitive_keys) for item in data]
    else:
        # Primitive types (str, int, float, bool, None) - return as-is
        return data


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Sanitize HTTP headers by filtering sensitive headers.

    Args:
        headers: HTTP headers dictionary

    Returns:
        Sanitized headers dictionary
    """
    sanitized = {}
    for key, value in headers.items():
        key_lower = key.lower()
        # Check if header is sensitive
        if any(
            key_lower.startswith(prefix.lower()) for prefix in SENSITIVE_HEADER_PREFIXES
        ) or _is_sensitive_key(key):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = value
    return sanitized


def sanitize_request_body(body: str) -> Dict[str, Any]:
    """Parse and sanitize JSON request body.

    Args:
        body: Request body as JSON string

    Returns:
        Sanitized request body as dictionary, or error dict if parsing fails
    """
    try:
        parsed = json.loads(body) if body else {}
        return sanitize_dict(parsed)
    except (json.JSONDecodeError, TypeError):
        # If parsing fails, return sanitized string representation
        return {"raw_body": "[REDACTED]" if len(body) > 0 else ""}


OAUTH_QUERY_REDACT_KEYS = {
    "code",
    "code_verifier",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
}


def summarize_query_string(query_string: str) -> Dict[str, Any]:
    """Summarize a query string without leaking OAuth secrets."""
    from urllib.parse import parse_qsl, urlparse

    params = dict(parse_qsl(query_string, keep_blank_values=True))
    redirect_uri = params.get("redirect_uri", "")
    parsed_redirect = urlparse(redirect_uri) if redirect_uri else None
    safe_params: Dict[str, Any] = {}
    for key, value in params.items():
        if key in OAUTH_QUERY_REDACT_KEYS:
            safe_params[key] = "[REDACTED]"
        elif key == "state":
            safe_params["state_length"] = len(value)
            safe_params["state_present"] = bool(value)
        elif key == "code_challenge":
            safe_params["code_challenge_present"] = bool(value)
            safe_params["code_challenge_length"] = len(value)
        elif key in {"client_id", "scope", "response_type", "code_challenge_method"}:
            safe_params[key] = value
        else:
            safe_params[key] = value

    return {
        "query_keys": sorted(params.keys()),
        "query_params_safe": safe_params,
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
    }


def summarize_response_body(body: str, content_type: str = "") -> Dict[str, Any]:
    """Summarize a response body for structured logging."""
    content_type_lower = content_type.lower()
    if not body:
        return {"response_body_kind": "empty", "response_body_length": 0}

    if "text/html" in content_type_lower:
        return {
            "response_body_kind": "html",
            "response_body_length": len(body),
            "response_body_preview": body[:200],
        }

    if "application/json" in content_type_lower or body.lstrip().startswith("{"):
        parsed = sanitize_response_body(body)
        return {
            "response_body_kind": "json",
            "response_body_length": len(body),
            "response_body": parsed,
        }

    return {
        "response_body_kind": "text",
        "response_body_length": len(body),
        "response_body_preview": body[:200],
    }


def summarize_response_headers(headers: Dict[str, str]) -> Dict[str, Any]:
    """Summarize response headers without leaking secrets."""
    sanitized = sanitize_headers(headers)
    location = sanitized.get("Location") or sanitized.get("location")
    summary: Dict[str, Any] = {
        "response_header_keys": sorted(headers.keys()),
        "response_headers": sanitized,
        "sets_cookie": any(
            key.lower() == "set-cookie" for key in headers.keys()
        ),
        "has_location": bool(location),
    }
    if location:
        from urllib.parse import parse_qsl, urlparse

        parsed = urlparse(location)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        summary["location_host"] = parsed.netloc
        summary["location_path"] = parsed.path
        summary["location_query_keys"] = sorted(params.keys())
        summary["location_has_code"] = bool(params.get("code"))
        summary["location_has_error"] = bool(params.get("error"))
        summary["location_has_state"] = bool(params.get("state"))
    return summary


def format_http_exchange_log(
    request_id: str,
    http_method: str,
    request_path: str,
    query_string: str,
    request_headers: Dict[str, str],
    request_body: str,
    *,
    phase: str,
    status_code: Optional[int] = None,
    response_headers: Optional[Dict[str, str]] = None,
    response_body: Optional[str] = None,
    duration_ms: Optional[float] = None,
    oauth_event: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a structured log entry for an HTTP request or response."""
    log_data: Dict[str, Any] = {
        "request_id": request_id,
        "http_method": http_method,
        "request_path": request_path,
        "phase": phase,
        "request_headers": sanitize_headers(request_headers),
        **summarize_query_string(query_string),
    }

    if request_body:
        content_type = request_headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            from urllib.parse import parse_qsl

            form = dict(parse_qsl(request_body, keep_blank_values=True))
            log_data["request_body"] = {
                key: "[REDACTED]" if key in OAUTH_QUERY_REDACT_KEYS else value
                for key, value in form.items()
            }
            log_data["request_body_kind"] = "form"
            log_data["request_body_keys"] = sorted(form.keys())
        else:
            log_data["request_body"] = sanitize_request_body(request_body)

    if status_code is not None:
        log_data["response_status"] = status_code
    if response_headers is not None:
        log_data.update(summarize_response_headers(response_headers))
        content_type = response_headers.get("Content-Type", "")
        if response_body is not None:
            log_data.update(
                summarize_response_body(response_body, content_type=content_type)
            )
    if duration_ms is not None:
        log_data["duration_ms"] = round(duration_ms, 2)
    if oauth_event:
        log_data["oauth_event"] = oauth_event
    if extra:
        log_data.update(extra)
    return log_data


def sanitize_response_body(body: str) -> Dict[str, Any]:
    """Parse and sanitize JSON response body.

    Args:
        body: Response body as JSON string

    Returns:
        Sanitized response body as dictionary, or error dict if parsing fails
    """
    try:
        parsed = json.loads(body) if body else {}
        return sanitize_dict(parsed)
    except (json.JSONDecodeError, TypeError):
        # If parsing fails, return sanitized string representation
        return {"raw_body": "[REDACTED]" if len(body) > 0 else ""}


def format_request_log(
    request_id: str,
    http_method: str,
    request_path: str,
    headers: Dict[str, str],
    body: str,
    lambda_context: Optional[Any] = None,
) -> Dict[str, Any]:
    """Format structured request log entry.

    Args:
        request_id: Request ID (from Lambda context)
        http_method: HTTP method (GET, POST, etc.)
        request_path: Request path/URL
        headers: HTTP headers
        body: Request body
        lambda_context: Optional Lambda context for metadata

    Returns:
        Dictionary with structured log data
    """
    log_data = {
        "request_id": request_id,
        "http_method": http_method,
        "request_path": request_path,
        "request_headers": sanitize_headers(headers),
        "request_body": sanitize_request_body(body),
    }

    # Add Lambda context metadata if available
    if lambda_context:
        log_data["lambda_function_name"] = getattr(
            lambda_context, "function_name", None
        )
        log_data["lambda_memory_limit"] = getattr(
            lambda_context, "memory_limit_in_mb", None
        )
        log_data["lambda_remaining_time_ms"] = getattr(
            lambda_context, "get_remaining_time_in_millis", lambda: None
        )()

    return log_data


def format_response_log(
    request_id: str,
    status_code: int,
    headers: Dict[str, str],
    body: str,
    duration_ms: float,
    success: bool = True,
) -> Dict[str, Any]:
    """Format structured response log entry.

    Args:
        request_id: Request ID
        status_code: HTTP status code
        headers: Response headers
        body: Response body
        duration_ms: Processing duration in milliseconds
        success: Whether request was successful

    Returns:
        Dictionary with structured log data
    """
    return {
        "request_id": request_id,
        "response_status": status_code,
        "response_headers": sanitize_headers(headers),
        "response_body": sanitize_response_body(body),
        "duration_ms": round(duration_ms, 2),
        "success": success,
    }


def format_jsonrpc_request_log(
    request_id: Optional[Any],
    method: str,
    params: Dict[str, Any],
    is_notification: bool = False,
) -> Dict[str, Any]:
    """Format structured JSON-RPC request log entry.

    Args:
        request_id: JSON-RPC request ID
        method: JSON-RPC method name
        params: JSON-RPC parameters
        is_notification: Whether this is a notification (no response)

    Returns:
        Dictionary with structured log data
    """
    return {
        "jsonrpc_request_id": request_id,
        "jsonrpc_method": method,
        "jsonrpc_params": sanitize_dict(params),
        "is_notification": is_notification,
    }


def format_jsonrpc_response_log(
    request_id: Optional[Any],
    method: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
    duration_ms: float = 0.0,
) -> Dict[str, Any]:
    """Format structured JSON-RPC response log entry.

    Args:
        request_id: JSON-RPC request ID
        method: JSON-RPC method name
        result: JSON-RPC result (if successful)
        error: JSON-RPC error (if failed)
        duration_ms: Processing duration in milliseconds

    Returns:
        Dictionary with structured log data
    """
    log_data = {
        "jsonrpc_request_id": request_id,
        "jsonrpc_method": method,
        "duration_ms": round(duration_ms, 2),
    }

    if error:
        log_data["jsonrpc_error"] = sanitize_dict(error)
        log_data["success"] = False
    else:
        log_data["jsonrpc_result"] = sanitize_dict(result) if result else None
        log_data["success"] = True

    return log_data
