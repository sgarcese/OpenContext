"""Base open data plugin for OpenContext.

This module defines :class:`BaseOpenDataPlugin`, a shared base class that
centralizes the HTTP client lifecycle, retry policy, HTTP error translation,
tool dispatch with required-argument validation, and record formatting that
the CKAN/Socrata/ArcGIS plugins currently copy-paste. Concrete plugins
subclass it, declare ``config_class`` and ``tool_handlers()``, and implement
the remaining ``DataPlugin`` abstract methods.
"""

import logging
import re
from collections.abc import Callable
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config_base import BasePluginConfig
from core.interfaces import DataPlugin, ToolResult

logger = logging.getLogger(__name__)

# Safe SQL identifier: letters, digits, underscores; must not start with a
# digit; max 64 chars. Used by build_where_clause to reject field names that
# could smuggle SQL fragments.
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

HTTP_RETRY = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_not_exception_type((RuntimeError, httpx.HTTPStatusError)),
)


class ToolHandler:
    """Descriptor for a single tool exposed by a plugin.

    Attributes:
        handler: Async callable taking an arguments dict and returning either
            a :class:`ToolResult` or a ``str`` (which is wrapped into a
            successful ``ToolResult``).
        required_args: Argument names that must be present and truthy before
            the handler runs.
    """

    __slots__ = ("handler", "required_args")

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], Any],
        required_args: tuple[str, ...] = (),
    ) -> None:
        """Initialize a ToolHandler.

        Args:
            handler: Async callable taking the arguments dict.
            required_args: Tuple of argument names that must be present and
                truthy before the handler is invoked.
        """
        self.handler = handler
        self.required_args = required_args


class BaseOpenDataPlugin(DataPlugin):
    """Shared base class for open data plugins.

    Subclasses set :attr:`config_class` to a :class:`BasePluginConfig`
    subclass, implement :meth:`tool_handlers` to declare their tools, and
    fill in the remaining ``DataPlugin`` abstract methods
    (:meth:`initialize`, :meth:`get_tools`, :meth:`health_check`, plus the
    data-access methods).
    """

    config_class: type[BasePluginConfig] = BasePluginConfig

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the plugin and eagerly validate its configuration.

        Args:
            config: Plugin configuration dictionary (from config.yaml).
        """
        super().__init__(config)
        self.plugin_config: BasePluginConfig = self.config_class(**config)
        self._clients: list[httpx.AsyncClient] = []

    def _create_http_client(self, **kwargs: Any) -> httpx.AsyncClient:
        """Create an :class:`httpx.AsyncClient` and track it for shutdown.

        Args:
            **kwargs: Forwarded to ``httpx.AsyncClient``.

        Returns:
            The created async HTTP client.
        """
        client = httpx.AsyncClient(**kwargs)
        self._clients.append(client)
        return client

    async def shutdown(self) -> None:
        """Close all tracked HTTP clients and mark the plugin uninitialized."""
        for client in self._clients:
            try:
                await client.aclose()
            except Exception as e:
                logger.warning(f"Error closing HTTP client: {e}")
        self._clients.clear()
        self._initialized = False
        logger.info(f"{self.plugin_name} plugin shut down")

    def _raise_http_error(self, exc: httpx.HTTPStatusError, context: str = "") -> None:
        """Translate an :class:`httpx.HTTPStatusError` into a RuntimeError.

        Attempts to extract a human-readable message from the response JSON
        (``message`` key, or CKAN-style nested ``error`` dict); falls back to
        the raw status text.

        Args:
            exc: The HTTP status error raised by ``raise_for_status``.
            context: Optional context label (e.g. ``"Discovery API"``).

        Raises:
            RuntimeError: Always, chained from ``exc``.
        """
        status_code = exc.response.status_code
        msg: str | None = None
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                if body.get("message"):
                    msg = body.get("message")
                else:
                    err = body.get("error")
                    if isinstance(err, dict):
                        msg = err.get("message", str(err))
                    elif err is not None:
                        msg = str(err)
        except (ValueError, TypeError):
            pass

        if not msg:
            msg = exc.response.text or f"HTTP {status_code}"

        portal = f"{self.plugin_config.city_name} OpenData portal"
        prefix = f"Error{context} on" if context else "Error on"
        raise RuntimeError(f"{prefix} {portal}: {msg} (HTTP {status_code})") from exc

    def tool_handlers(self) -> dict[str, ToolHandler]:
        """Return the mapping of tool name to :class:`ToolHandler`.

        Subclasses must override this to expose their tools via
        :meth:`execute_tool`.

        Returns:
            Dict mapping tool name (without plugin prefix) to ToolHandler.
        """
        return {}

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        """Dispatch a tool call to the matching registered handler.

        Args:
            tool_name: Name of the tool (without plugin prefix).
            arguments: Tool input arguments.

        Returns:
            ``ToolResult`` with content and success flag. Unknown tools,
            missing required arguments, and handler exceptions are all
            translated into unsuccessful ``ToolResult`` objects.
        """
        handlers = self.tool_handlers()
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                content=[],
                success=False,
                error_message=f"Unknown tool: {tool_name}",
            )

        for arg in handler.required_args:
            if not arguments.get(arg):
                return ToolResult(
                    content=[],
                    success=False,
                    error_message=f"{arg} is required",
                )

        try:
            result = await handler.handler(arguments)
            if isinstance(result, ToolResult):
                return result
            if isinstance(result, str):
                return ToolResult(
                    content=[{"type": "text", "text": result}],
                    success=True,
                )
            return ToolResult(
                content=[{"type": "text", "text": str(result)}],
                success=True,
            )
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )

    def format_records(
        self,
        records: list[dict[str, Any]],
        *,
        max_display: int = 10,
        header: str | None = None,
        skip_keys: frozenset = frozenset({"_id"}),
    ) -> str:
        """Format a list of record dicts for user display.

        Replicates the ``Record N:`` style used by the existing plugins, with
        a ``... and X more record(s)`` suffix and a ``No records found.``
        empty case.

        Args:
            records: List of record dictionaries.
            max_display: Maximum number of records to render in full.
            header: Optional leading header line (e.g. ``"Found N record(s)"``).
            skip_keys: Record keys to omit from the output.

        Returns:
            Formatted string.
        """
        if not records:
            return "No records found."

        lines: list[str] = []
        if header:
            lines.append(header)
            lines.append("")

        for i, record in enumerate(records[:max_display], 1):
            lines.append(f"Record {i}:")
            for key, value in record.items():
                if key in skip_keys:
                    continue
                lines.append(f"  {key}: {value}")
            lines.append("")

        if len(records) > max_display:
            lines.append(f"... and {len(records) - max_display} more record(s)")

        return "\n".join(lines)

    @staticmethod
    def build_where_clause(filters: dict[str, Any]) -> str:
        """Build a SQL ``WHERE`` clause from a field/value filter dict.

        Strings are escaped by doubling single quotes, ``None`` becomes
        ``IS NULL``, and other values are rendered as-is. Conditions are
        joined with ``AND``. Field names must be plain identifiers
        (letters, digits, underscores, not starting with a digit); anything
        else raises so SQL cannot be smuggled in through field names.

        Args:
            filters: Mapping of field name to filter value.

        Returns:
            The ``WHERE`` clause body (without the leading ``WHERE``
            keyword), or an empty string when ``filters`` is empty.

        Raises:
            ValueError: If a field name is not a safe identifier.
        """
        if not filters:
            return ""
        conditions: list[str] = []
        for field, value in filters.items():
            if not isinstance(field, str) or not _SAFE_IDENTIFIER.match(field):
                raise ValueError(f"Invalid filter field name: {field!r}")
            if isinstance(value, str):
                escaped = value.replace("'", "''")
                conditions.append(f"{field} = '{escaped}'")
            elif value is None:
                conditions.append(f"{field} IS NULL")
            else:
                conditions.append(f"{field} = {value}")
        return " AND ".join(conditions)

    # The following DataPlugin abstract methods remain abstract; subclasses
    # implement them. They are re-declared here only to document intent and
    # keep type checkers happy about the partial-implementation pattern.
