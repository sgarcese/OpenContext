"""CKAN plugin implementation for OpenContext.

This plugin provides access to CKAN-based open data portals.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from core.base_plugin import BaseOpenDataPlugin, HTTP_RETRY, ToolHandler
from core.interfaces import PluginType, ToolDefinition, ToolResult
from plugins.ckan.config_schema import CKANPluginConfig
from plugins.ckan.sql_validator import SQLValidator

logger = logging.getLogger(__name__)

# Whitelists for SQL identifiers and metric expressions assembled by
# aggregate_data, to prevent SQL injection through field names / aliases.
# Ported from thealphacubicle/OpenContext (Feature/security update #37).
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_SAFE_METRIC_EXPR = re.compile(
    r"^(count\(\s*\*?\s*\)|(?:sum|avg|min|max|stddev|variance)\(\s*[a-zA-Z_][a-zA-Z0-9_]{0,63}\s*\))$",
    re.IGNORECASE,
)


def _validate_identifier(name: str) -> None:
    """Validate that ``name`` is a safe SQL identifier.

    Args:
        name: Identifier to validate.

    Raises:
        ValueError: If ``name`` is not a safe identifier.
    """
    if not isinstance(name, str) or not _SAFE_IDENTIFIER.match(name):
        raise ValueError(f"Invalid identifier: {name!r}")


def _validate_metric_expr(expr: str) -> None:
    """Validate that ``expr`` is a safe aggregate metric expression.

    Args:
        expr: Metric expression to validate (e.g. ``count(*)`` or ``avg(field)``).

    Raises:
        ValueError: If ``expr`` is not an allowed aggregate expression.
    """
    if not isinstance(expr, str) or not _SAFE_METRIC_EXPR.match(expr):
        raise ValueError(f"Invalid metric expression: {expr!r}")


class CKANPlugin(BaseOpenDataPlugin):
    """Plugin for accessing CKAN-based open data portals.

    This plugin implements the :class:`DataPlugin` interface on top of
    :class:`BaseOpenDataPlugin` and provides tools for searching datasets,
    retrieving dataset metadata, and querying data.
    """

    plugin_name = "ckan"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    config_class = CKANPluginConfig

    async def initialize(self) -> bool:
        """Initialize CKAN plugin and test connection.

        Returns:
            True if initialization succeeded
        """
        try:
            # Create HTTP client via the shared helper so it is tracked for
            # shutdown by the base class.
            headers = {}
            if self.plugin_config.api_key:
                headers["Authorization"] = self.plugin_config.api_key

            self.client = self._create_http_client(
                base_url=self.plugin_config.base_url,
                headers=headers,
                timeout=self.plugin_config.timeout,
            )

            # Test connection
            response = await self._call_ckan_api("status_show", {})
            if response.get("success"):
                self._initialized = True
                logger.info(
                    f"CKAN plugin initialized successfully for {self.plugin_config.city_name}"
                )
                return True
            else:
                logger.error("CKAN API connection test failed")
                return False

        except Exception as e:
            logger.error(f"Failed to initialize CKAN plugin: {e}", exc_info=True)
            return False

    def _parse_ckan_error(
        self, response_body: Dict[str, Any], context: str = ""
    ) -> str:
        """Extract human-readable error from CKAN API response body."""
        if response_body.get("success") is True:
            return ""
        err = response_body.get("error", {})
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        portal = f" on {self.plugin_config.city_name} OpenData portal"
        base = f"{msg}{portal}" if msg else f"Unknown error{portal}"
        return f"{context}: {base}" if context else base

    @HTTP_RETRY
    async def _call_ckan_api(self, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Call CKAN API action.

        Args:
            action: CKAN action name (e.g., "package_search")
            data: Action parameters

        Returns:
            CKAN API response

        Raises:
            RuntimeError: On HTTP errors or when CKAN returns success: false
        """
        if not self.client:
            raise RuntimeError("Plugin not initialized")

        url = f"/api/3/action/{action}"
        portal = f"{self.plugin_config.city_name} OpenData portal"

        try:
            response = await self.client.post(url, json=data)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            try:
                body = e.response.json()
                ckan_msg = self._parse_ckan_error(body, "")
                if ckan_msg:
                    raise RuntimeError(f"Error: {ckan_msg} (HTTP {status_code})") from e
            except ValueError:
                pass
            param_hint = ""
            if "resource_id" in data:
                param_hint = f" Resource '{data.get('resource_id')}'"
            elif "id" in data:
                param_hint = f" Dataset '{data.get('id')}'"
            raise RuntimeError(
                f"Error:{param_hint} not found on {portal} (HTTP {status_code})"
            ) from e

        result = response.json()

        if result.get("success") is False:
            msg = self._parse_ckan_error(result, "")
            raise RuntimeError(f"Error: {msg}" if msg else f"API error on {portal}")

        return result

    def get_tools(self) -> List[ToolDefinition]:
        """Get list of tools provided by CKAN plugin.

        Returns:
            List of tool definitions
        """
        return [
            ToolDefinition(
                name="search_datasets",
                description=f"Search for datasets in {self.plugin_config.city_name}'s open data portal",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query string",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 20)",
                            "default": 20,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name="get_dataset",
                description=f"Get detailed information about a specific dataset from {self.plugin_config.city_name}'s open data portal",
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": "Dataset ID or name",
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="query_data",
                description=f"Query data from a specific resource in {self.plugin_config.city_name}'s open data portal",
                input_schema={
                    "type": "object",
                    "properties": {
                        "resource_id": {
                            "type": "string",
                            "description": "Resource ID to query",
                        },
                        "filters": {
                            "type": "object",
                            "description": "Optional filters (field: value pairs)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of records (default: 100)",
                            "default": 100,
                        },
                    },
                    "required": ["resource_id"],
                },
            ),
            ToolDefinition(
                name="get_schema",
                description=f"Get schema information for a resource in {self.plugin_config.city_name}'s open data portal",
                input_schema={
                    "type": "object",
                    "properties": {
                        "resource_id": {
                            "type": "string",
                            "description": "Resource ID",
                        },
                    },
                    "required": ["resource_id"],
                },
            ),
            ToolDefinition(
                name="execute_sql",
                description="""Execute raw PostgreSQL SELECT query.

⚠️ Advanced users only. For complex queries requiring full SQL.

Security: Only SELECT allowed. INSERT/UPDATE/DELETE blocked.

Examples:
- Window functions: RANK() OVER (...)
- CTEs: WITH subquery AS (...)
- Complex aggregations: PERCENTILE_CONT(0.5) WITHIN GROUP

Resource IDs must be double-quoted: FROM "uuid-here"
""",
                input_schema={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "PostgreSQL SELECT statement",
                        },
                    },
                    "required": ["sql"],
                },
            ),
            ToolDefinition(
                name="aggregate_data",
                description=f"""Aggregate data with GROUP BY from {self.plugin_config.city_name}'s open data portal.

Prerequisites: get_schema for field names

Examples:
- Count by field: group_by=["neighborhood"], metrics={{count: "count(*)"}}
- Multiple metrics: metrics={{total: "count(*)", avg: "avg(field)"}}
- With filters: filters={{"status": "Open"}}
- Having: having={{"count(*)": ">= 5"}} (string values may include the
  operator; numeric values default to ">")

Supports: count(*), sum(), avg(), min(), max(), stddev()
""",
                input_schema={
                    "type": "object",
                    "properties": {
                        "resource_id": {"type": "string"},
                        "group_by": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "metrics": {"type": "object"},
                        "filters": {"type": "object"},
                        "having": {"type": "object"},
                        "order_by": {"type": "string"},
                        "limit": {"type": "integer", "default": 100},
                    },
                    "required": ["resource_id", "metrics"],
                },
            ),
        ]

    def tool_handlers(self) -> Dict[str, ToolHandler]:
        """Return the mapping of tool name to :class:`ToolHandler`.

        Returns:
            Dict mapping tool name (without plugin prefix) to ToolHandler.
        """
        return {
            "search_datasets": ToolHandler(
                handler=self._tool_search_datasets,
            ),
            "get_dataset": ToolHandler(
                handler=self._tool_get_dataset,
                required_args=("dataset_id",),
            ),
            "query_data": ToolHandler(
                handler=self._tool_query_data,
                required_args=("resource_id",),
            ),
            "get_schema": ToolHandler(
                handler=self._tool_get_schema,
                required_args=("resource_id",),
            ),
            "execute_sql": ToolHandler(
                handler=self._tool_execute_sql,
                required_args=("sql",),
            ),
            "aggregate_data": ToolHandler(
                handler=self._tool_aggregate_data,
                required_args=("resource_id", "metrics"),
            ),
        }

    async def _tool_search_datasets(self, arguments: Dict[str, Any]) -> ToolResult:
        query = arguments.get("query", "")
        limit = arguments.get("limit", 20)
        datasets = await self.search_datasets(query, limit)
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": self._format_search_results(datasets),
                }
            ],
            success=True,
        )

    async def _tool_get_dataset(self, arguments: Dict[str, Any]) -> ToolResult:
        dataset = await self.get_dataset(arguments["dataset_id"])
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": self._format_dataset(dataset),
                }
            ],
            success=True,
        )

    async def _tool_query_data(self, arguments: Dict[str, Any]) -> ToolResult:
        filters = arguments.get("filters", {})
        limit = arguments.get("limit", 100)
        data = await self.query_data(arguments["resource_id"], filters, limit)
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": self._format_query_results(data, limit),
                }
            ],
            success=True,
        )

    async def _tool_get_schema(self, arguments: Dict[str, Any]) -> ToolResult:
        schema = await self.get_schema(arguments["resource_id"])
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": self._format_schema(schema),
                }
            ],
            success=True,
        )

    async def _tool_execute_sql(self, arguments: Dict[str, Any]) -> ToolResult:
        result = await self.execute_sql(arguments["sql"])
        if result.get("error"):
            return ToolResult(
                content=[],
                success=False,
                error_message=result.get("message", "SQL execution failed"),
            )
        records = result.get("records", [])
        fields = result.get("fields", [])
        formatted_text = self._format_sql_results(records, fields)
        return ToolResult(
            content=[{"type": "text", "text": formatted_text}],
            success=True,
        )

    async def _tool_aggregate_data(self, arguments: Dict[str, Any]) -> ToolResult:
        result = await self.aggregate_data(
            resource_id=arguments["resource_id"],
            group_by=arguments.get("group_by", []),
            metrics=arguments["metrics"],
            filters=arguments.get("filters"),
            having=arguments.get("having"),
            order_by=arguments.get("order_by"),
            limit=arguments.get("limit", 100),
        )
        if result.get("error"):
            return ToolResult(
                content=[],
                success=False,
                error_message=result.get("message", "Aggregation failed"),
            )
        formatted = self._format_sql_results(
            result.get("records", []), result.get("fields", [])
        )
        return ToolResult(
            content=[{"type": "text", "text": formatted}], success=True
        )

    async def search_datasets(
        self, query: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search for datasets matching a query.

        Args:
            query: Search query string
            limit: Maximum number of results

        Returns:
            List of dataset metadata dictionaries
        """
        response = await self._call_ckan_api(
            "package_search", {"q": query, "rows": limit}
        )
        return response.get("result", {}).get("results", [])

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """Get detailed metadata for a specific dataset.

        Args:
            dataset_id: Dataset ID or name

        Returns:
            Dataset metadata dictionary
        """
        response = await self._call_ckan_api("package_show", {"id": dataset_id})
        return response.get("result", {})

    async def query_data(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query data from a specific resource.

        Args:
            resource_id: Resource ID
            filters: Optional filters (field: value pairs)
            limit: Maximum number of records

        Returns:
            List of data records
        """
        params = {"resource_id": resource_id, "limit": limit}

        # Convert filters to CKAN filter format
        if filters:
            for field, value in filters.items():
                params[f"filters[{field}]"] = value

        response = await self._call_ckan_api("datastore_search", params)
        return response.get("result", {}).get("records", [])

    async def get_schema(self, resource_id: str) -> Dict[str, Any]:
        """Get schema information for a resource.

        Args:
            resource_id: Resource ID

        Returns:
            Schema information dictionary
        """
        # Get schema by calling datastore_search with limit=0
        response = await self._call_ckan_api(
            "datastore_search", {"resource_id": resource_id, "limit": 0}
        )
        return response.get("result", {}).get("fields", [])

    async def execute_sql(self, sql: str) -> Dict[str, Any]:
        """Execute raw PostgreSQL SELECT query with security validation.

        Args:
            sql: PostgreSQL SELECT statement

        Returns:
            Dictionary with success flag, records, fields, or error message
        """
        # Validate SQL
        is_valid, error = SQLValidator.validate_query(sql)
        if not is_valid:
            return {"error": True, "message": error}

        # Log SQL execution (truncated for security)
        logger.info("Executing SQL", extra={"sql": sql[:500]})

        # Execute
        try:
            result = await self._call_ckan_api("datastore_search_sql", {"sql": sql})
            if not result.get("success", True):
                return {
                    "error": True,
                    "message": self._parse_ckan_error(result, "SQL execution failed"),
                }
            return {
                "success": True,
                "records": result.get("result", {}).get("records", []),
                "fields": result.get("result", {}).get("fields", []),
            }
        except Exception as e:
            logger.error(f"SQL execution failed: {e}", exc_info=True)
            return {"error": True, "message": str(e)}

    async def aggregate_data(
        self,
        resource_id: str,
        group_by: List[str],
        metrics: Dict[str, str],
        filters: Optional[Dict[str, Any]] = None,
        having: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Aggregate data with GROUP BY.

        Args:
            resource_id: Resource ID (must be valid UUID)
            group_by: List of fields to group by
            metrics: Dictionary of metric_name: sql_expression (e.g., {"count": "count(*)"})
            filters: Optional WHERE clause filters (field: value pairs)
            having: Optional HAVING clause filters (expression: value pairs).
                String values may include their own operator (e.g.
                ``{">= 5"}``-style); numeric values default to the ``>``
                operator for backward compatibility.
            order_by: Optional field to order by
            limit: Maximum number of results

        Returns:
            Dictionary with success flag, records, fields, or error message
        """
        # Validate all identifiers/expressions before building SQL to prevent
        # SQL injection through field names, metric aliases/expressions, or
        # order_by. Ported from thealphacubicle/OpenContext (security update #37).
        try:
            for field in group_by or []:
                _validate_identifier(field)
            for alias, expr in metrics.items():
                _validate_identifier(alias)
                _validate_metric_expr(expr)
            if filters:
                for field in filters:
                    _validate_identifier(field)
            if having:
                for expr in having:
                    # HAVING keys are aggregate expressions like "count(*)";
                    # validate them as metric expressions.
                    _validate_metric_expr(expr)
            if order_by:
                # order_by may be prefixed with '-' for descending.
                order_field = order_by[1:] if order_by.startswith("-") else order_by
                _validate_identifier(order_field)
        except ValueError as e:
            return {"error": True, "message": str(e)}

        # SELECT
        select_fields = ", ".join(group_by) if group_by else ""
        select_metrics = ", ".join(
            [f"{expr} as {name}" for name, expr in metrics.items()]
        )
        select_clause = (
            f"{select_fields}, {select_metrics}" if select_fields else select_metrics
        )

        # WHERE
        where_body = self.build_where_clause(filters) if filters else ""
        where_clause = f"WHERE {where_body}" if where_body else ""

        # GROUP BY
        group_clause = f"GROUP BY {', '.join(group_by)}" if group_by else ""

        # HAVING
        having_clause = ""
        if having:
            conditions = []
            for expr, value in having.items():
                if isinstance(value, str):
                    # Value carries its own operator (e.g. ">= 5").
                    conditions.append(f"{expr} {value}")
                else:
                    # Numeric value: default to the documented ">" operator.
                    conditions.append(f"{expr} > {value}")
            having_clause = "HAVING " + " AND ".join(conditions)

        # ORDER BY
        order_clause = f"ORDER BY {order_by}" if order_by else ""

        # Build SQL
        sql = f'SELECT {select_clause} FROM "{resource_id}" {where_clause} {group_clause} {having_clause} {order_clause} LIMIT {limit}'.strip()

        return await self.execute_sql(sql)

    async def health_check(self) -> bool:
        """Check if CKAN API is accessible.

        Returns:
            True if healthy
        """
        try:
            response = await self._call_ckan_api("status_show", {})
            return response.get("success", False)
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    def _format_search_results(self, datasets: List[Dict[str, Any]]) -> str:
        """Format search results for user display."""
        if not datasets:
            return f"No datasets found in {self.plugin_config.city_name}'s open data portal."

        lines = [
            f"Found {len(datasets)} dataset(s) in {self.plugin_config.city_name}'s open data portal:\n"
        ]

        for i, dataset in enumerate(datasets, 1):
            title = dataset.get("title", "Untitled")
            dataset_id = dataset.get("id", "unknown")
            notes = (
                dataset.get("notes", "")[:100] + "..."
                if dataset.get("notes")
                else "No description"
            )

            lines.append(f"{i}. {title}")
            lines.append(f"   ID: {dataset_id}")
            lines.append(f"   Description: {notes}")
            lines.append(
                f"   Portal: {self.plugin_config.portal_url}/dataset/{dataset_id}"
            )
            lines.append("")

        lines.append(
            f"View all datasets at: {self.plugin_config.portal_url}\n"
            f"Use get_dataset tool with a dataset ID to get more details."
        )

        return "\n".join(lines)

    def _format_dataset(self, dataset: Dict[str, Any]) -> str:
        """Format dataset metadata for user display."""
        title = dataset.get("title", "Untitled")
        dataset_id = dataset.get("id", "unknown")
        notes = dataset.get("notes", "No description")
        organization = dataset.get("organization", {}).get("title", "Unknown")
        resources = dataset.get("resources", [])

        lines = [
            f"Dataset: {title}",
            f"ID: {dataset_id}",
            f"Organization: {organization}",
            f"Description: {notes}",
            "",
            f"Portal URL: {self.plugin_config.portal_url}/dataset/{dataset_id}",
            "",
        ]

        if resources:
            lines.append(f"Resources ({len(resources)}):")
            for i, resource in enumerate(resources, 1):
                res_name = resource.get("name", "Unnamed")
                res_id = resource.get("id", "unknown")
                res_format = resource.get("format", "unknown")
                lines.append(f"  {i}. {res_name} ({res_format})")
                lines.append(f"     Resource ID: {res_id}")
                lines.append(
                    f"     Use query_data tool with resource_id='{res_id}' to query this data"
                )
        else:
            lines.append("No resources available for this dataset.")

        return "\n".join(lines)

    def _format_query_results(self, records: List[Dict[str, Any]], limit: int) -> str:
        """Format query results for user display."""
        if not records:
            return "No records found matching the query."

        return self.format_records(
            records,
            max_display=5,
            header=f"Found {len(records)} record(s) (showing up to {limit}):",
        )

    def _format_schema(self, fields: List[Dict[str, Any]]) -> str:
        """Format schema information for user display."""
        if not fields:
            return "No schema information available."

        lines = ["Schema fields:"]
        for field in fields:
            field_id = field.get("id", "unknown")
            field_type = field.get("type", "unknown")
            field_info = field.get("info", {})
            description = field_info.get("label", "") if field_info else ""

            lines.append(f"  • {field_id} ({field_type})")
            if description:
                lines.append(f"    {description}")

        return "\n".join(lines)

    def _format_sql_results(
        self, records: List[Dict[str, Any]], fields: List[Dict[str, Any]]
    ) -> str:
        """Format SQL query results for user display.

        Args:
            records: List of record dictionaries
            fields: List of field metadata dictionaries

        Returns:
            Formatted string representation of results
        """
        if not records:
            return "No records found matching the SQL query."

        header_lines = [f"SQL Query Results: {len(records)} record(s)"]
        # Show field names if available
        if fields:
            field_names = [field.get("id", "unknown") for field in fields]
            header_lines.append(f"Fields: {', '.join(field_names)}")

        header = "\n".join(header_lines)
        return self.format_records(records, max_display=10, header=header)