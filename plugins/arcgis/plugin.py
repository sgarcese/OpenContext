"""ArcGIS Hub plugin implementation for OpenContext.

This plugin provides access to ArcGIS Hub open data catalogs
via the OGC API - Records (Hub Search API) and ArcGIS Feature Services.
"""

import logging
import re
from datetime import datetime
from typing import Any

import httpx

from core.base_plugin import HTTP_RETRY, BaseOpenDataPlugin, ToolHandler
from core.interfaces import PluginType, ToolDefinition, ToolResult
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.where_validator import WhereValidator

logger = logging.getLogger(__name__)


class ArcGISPlugin(BaseOpenDataPlugin):
    """Plugin for accessing ArcGIS Hub open data catalogs.

    This plugin implements the DataPlugin interface on top of
    :class:`BaseOpenDataPlugin` and provides tools for searching datasets,
    retrieving dataset metadata, querying Feature Services, and exploring
    catalog aggregations.
    """

    plugin_name = "arcgis"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    config_class = ArcGISPluginConfig

    QUERYABLE_TYPES = {
        "Feature Layer",
        "Feature Service",
        "Map Service",
        "Table",
    }

    async def initialize(self) -> bool:
        """Initialize ArcGIS Hub plugin and test connection.

        Returns:
            True if initialization succeeded
        """
        try:
            headers = {"Accept": "application/json"}
            feature_headers: dict[str, str] = {}
            if self.plugin_config.token:
                headers["Authorization"] = f"Bearer {self.plugin_config.token}"
                feature_headers["Authorization"] = f"Bearer {self.plugin_config.token}"

            # Create both clients via the shared helper so they are tracked
            # for shutdown by the base class.
            self.hub_client = self._create_http_client(
                base_url=self.plugin_config.portal_url,
                headers=headers,
                timeout=self.plugin_config.timeout,
            )

            self.feature_client = self._create_http_client(
                headers=feature_headers,
                timeout=self.plugin_config.timeout,
            )

            await self._call_hub_api("/api/search/v1/collections")

            self._initialized = True
            logger.info(
                f"ArcGIS Hub plugin initialized successfully for "
                f"{self.plugin_config.city_name}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to initialize ArcGIS Hub plugin: {e}", exc_info=True)
            return False

    @HTTP_RETRY
    async def _call_hub_api(self, path: str, **kwargs: Any) -> httpx.Response:
        """Call the ArcGIS Hub Search API via the hub client (GET).

        Args:
            path: API path.
            **kwargs: Additional request arguments forwarded to httpx.

        Returns:
            The httpx response (caller is responsible for json parsing as
            appropriate).

        Raises:
            RuntimeError: On HTTP status errors.
        """
        if not self.hub_client:
            raise RuntimeError("Plugin not initialized")

        try:
            response = await self.hub_client.get(path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            self._raise_http_error(e, " Hub Search API")

        return response

    @HTTP_RETRY
    async def _call_feature_service(
        self, url: str, params: dict[str, Any]
    ) -> httpx.Response:
        """Call an ArcGIS Feature Service endpoint via the feature client (GET).

        Args:
            url: Absolute Feature Service URL.
            params: Query parameters.

        Returns:
            The httpx response (caller is responsible for json parsing).

        Raises:
            RuntimeError: On HTTP status errors.
        """
        if not self.feature_client:
            raise RuntimeError("Plugin not initialized")

        try:
            response = await self.feature_client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            self._raise_http_error(e, " Feature Service")

        return response

    def get_tools(self) -> list[ToolDefinition]:
        """Get list of tools provided by ArcGIS Hub plugin.

        Returns:
            List of tool definitions
        """
        city = self.plugin_config.city_name
        return [
            ToolDefinition(
                name="search_datasets",
                description=f"Search for datasets in {city}'s ArcGIS Hub catalog",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Full-text search query",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name="get_dataset",
                description="Get metadata for a specific ArcGIS Hub dataset by ID",
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": "32-char hex Hub item ID",
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="get_aggregations",
                description=(
                    "Get facet counts for a field across the ArcGIS Hub catalog. "
                    "Useful for exploring available categories, types, or tags."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": (
                                "Field to aggregate. Available fields: "
                                '"type", "tags", "categories", "access"'
                            ),
                        },
                        "q": {
                            "type": "string",
                            "description": "Optional search query to scope the aggregation",
                        },
                    },
                    "required": ["field"],
                },
            ),
            ToolDefinition(
                name="get_schema",
                description=(
                    f"Get field schema for an ArcGIS Feature Service layer in {city}'s "
                    f"ArcGIS Hub catalog. Returns field names, types, and aliases "
                    f"directly usable in query_data where/out_fields. Provide the Hub "
                    f"dataset ID — the plugin resolves the Feature Service URL "
                    f"automatically (two-hop)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": "Hub item ID (same as get_dataset)",
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="query_data",
                description=(
                    "Query records from an ArcGIS Feature Service. Provide the Hub "
                    "dataset ID — the plugin resolves the Feature Service URL "
                    "automatically (two-hop). Use get_dataset first to confirm the "
                    "dataset has a queryable service URL."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": "Hub item ID (same as get_dataset)",
                        },
                        "where": {
                            "type": "string",
                            "description": "SQL WHERE clause for filtering",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated field names to return",
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of records (default: 100)",
                            "default": 100,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
        ]

    def tool_handlers(self) -> dict[str, ToolHandler]:
        """Return the mapping of tool name to :class:`ToolHandler`.

        Returns:
            Dict mapping tool name (without plugin prefix) to ToolHandler.
        """
        return {
            "search_datasets": ToolHandler(handler=self._tool_search_datasets),
            "get_dataset": ToolHandler(
                handler=self._tool_get_dataset, required_args=("dataset_id",)
            ),
            "get_aggregations": ToolHandler(
                handler=self._tool_get_aggregations, required_args=("field",)
            ),
            "get_schema": ToolHandler(
                handler=self._tool_get_schema, required_args=("dataset_id",)
            ),
            "query_data": ToolHandler(
                handler=self._tool_query_data, required_args=("dataset_id",)
            ),
        }

    async def _tool_search_datasets(self, arguments: dict[str, Any]) -> ToolResult:
        query = arguments.get("query", "")
        limit = arguments.get("limit", 10)
        datasets = await self.search_datasets(query, limit)
        return ToolResult(
            content=[{"type": "text", "text": self._format_search_results(datasets)}],
            success=True,
        )

    async def _tool_get_dataset(self, arguments: dict[str, Any]) -> ToolResult:
        dataset = await self.get_dataset(arguments["dataset_id"])
        return ToolResult(
            content=[{"type": "text", "text": self._format_dataset(dataset)}],
            success=True,
        )

    async def _tool_get_aggregations(self, arguments: dict[str, Any]) -> ToolResult:
        field = arguments["field"]
        q = arguments.get("q")
        buckets = await self.get_aggregations(field, q)
        return ToolResult(
            content=[{"type": "text", "text": self._format_aggregations(field, buckets)}],
            success=True,
        )

    async def _tool_get_schema(self, arguments: dict[str, Any]) -> ToolResult:
        schema = await self.get_schema(arguments["dataset_id"])
        return ToolResult(
            content=[{"type": "text", "text": self._format_schema(schema)}],
            success=True,
        )

    async def _tool_query_data(self, arguments: dict[str, Any]) -> ToolResult:
        dataset_id = arguments["dataset_id"]
        where = arguments.get("where", "1=1")
        out_fields = arguments.get("out_fields", "*")
        limit = arguments.get("limit", 100)
        records = await self._query_features(dataset_id, where, out_fields, limit)
        return ToolResult(
            content=[{"type": "text", "text": self._format_query_results(records, limit)}],
            success=True,
        )

    # ── DataPlugin abstract method implementations ──────────────────────

    async def search_datasets(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search for datasets matching a query.

        Args:
            query: Search query string
            limit: Maximum number of results

        Returns:
            List of dataset metadata dictionaries
        """
        response = await self._call_hub_api(
            "/api/search/v1/collections/all/items",
            params={"q": query, "limit": limit},
        )

        data = response.json()
        features = data.get("features", [])
        if not features:
            return []

        results = []
        for feature in features:
            props = feature.get("properties", {})
            results.append(self._extract_dataset_summary(props))
        return results

    async def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        """Get detailed metadata for a specific dataset.

        Args:
            dataset_id: Hub item ID

        Returns:
            Dataset metadata dictionary
        """
        response = await self._call_hub_api(
            f"/api/search/v1/collections/all/items/{dataset_id}",
        )

        feature = response.json()
        props = feature.get("properties", {})

        result = self._extract_dataset_summary(props)
        result.update(
            {
                "snippet": props.get("snippet", ""),
                "licenseInfo": props.get("licenseInfo", ""),
                "spatialReference": props.get("spatialReference", ""),
                "geometryType": props.get("geometryType", ""),
                "additionalResources": props.get("additionalResources", []),
                "numRecords": props.get("numRecords", None),
                "service_url": props.get("url", ""),
            }
        )
        return result

    async def get_schema(self, dataset_id: str) -> list[dict[str, Any]]:
        """Get field schema for a dataset's Feature Service layer.

        Resolves the Feature Service URL via :meth:`get_dataset` (two-hop),
        then fetches the layer metadata (``{service_url}/0?f=json``) and
        returns the ``fields`` list (name, type, alias).

        Args:
            dataset_id: Hub item ID

        Returns:
            List of field definition dictionaries (name, type, alias)

        Raises:
            ValueError: If the dataset has no queryable service URL.
        """
        dataset = await self.get_dataset(dataset_id)
        service_url = dataset.get("service_url")
        if not service_url:
            raise ValueError(
                f"Dataset {dataset_id} does not have a queryable Feature Service URL"
            )

        service_url = self._ensure_layer_url(service_url)
        meta_url = f"{service_url}?f=json"

        response = await self._call_feature_service(meta_url, {})

        data = response.json()
        fields = data.get("fields", [])
        return [
            {
                "name": f.get("name", ""),
                "type": f.get("type", ""),
                "alias": f.get("alias", ""),
            }
            for f in fields
        ]

    async def query_data(
        self,
        resource_id: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query data from a dataset (DataPlugin contract).

        Compiles ``field: value`` filters into an ArcGIS WHERE clause using
        :meth:`BaseOpenDataPlugin.build_where_clause`, validating each field
        identifier with :meth:`WhereValidator.scan_forbidden_keywords` to
        block SQL injection through field names. Defaults to ``"1=1"`` when
        no filters are supplied.

        Args:
            resource_id: Hub item ID
            filters: Optional field/value pairs compiled to a WHERE clause
            limit: Maximum number of records

        Returns:
            List of data records
        """
        where_clause = "1=1"
        if filters:
            for field in filters:
                forbidden = WhereValidator.scan_forbidden_keywords(field)
                if forbidden:
                    raise ValueError(f"Invalid field name: {forbidden}")
            built = self.build_where_clause(filters)
            if built:
                where_clause = built

        return await self._query_features(resource_id, where_clause, "*", limit)

    async def _query_features(
        self,
        dataset_id: str,
        where: str,
        out_fields: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Query records from an ArcGIS Feature Service (two-hop resolution).

        Args:
            dataset_id: Hub item ID
            where: SQL WHERE clause (validated via :class:`WhereValidator`)
            out_fields: Comma-separated field names to return
            limit: Maximum number of records

        Returns:
            List of feature attribute dicts
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")

        dataset = await self.get_dataset(dataset_id)
        service_url = dataset.get("service_url")
        ds_type = dataset.get("type", "")
        if not service_url:
            raise ValueError(
                f"Dataset {dataset_id} does not have a queryable Feature Service URL"
            )

        if ds_type and ds_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                f"Dataset type '{ds_type}' is not queryable. "
                f"query_data only supports: {', '.join(sorted(self.QUERYABLE_TYPES))}."
            )

        where_clause = WhereValidator.validate(where)
        service_url = self._ensure_layer_url(service_url)
        query_url = f"{service_url}/query"
        record_count = min(limit, 1000)
        params = {
            "where": where_clause,
            "outFields": out_fields,
            "resultRecordCount": record_count,
            "f": "json",
            "returnGeometry": "false",
        }

        response = await self._call_feature_service(query_url, params)

        try:
            data = response.json()
        except Exception as json_err:
            content_type = response.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type}). The dataset URL may not "
                f"point to a queryable ArcGIS Feature Service."
            ) from json_err

        error_in_body = data.get("error")
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            detail_str = "; ".join(details) if details else ""
            raise RuntimeError(
                f"Feature Service query failed (code {code}): {msg}"
                + (f" — {detail_str}" if detail_str else "")
            )

        features = data.get("features", [])
        if not features:
            return []

        return [f.get("attributes", {}) for f in features]

    # ── Aggregations (standalone helper, not a DataPlugin method) ───────

    async def get_aggregations(
        self, field: str, q: str | None = None
    ) -> list[dict[str, Any]]:
        """Get facet counts for a field across the ArcGIS Hub catalog.

        Args:
            field: Field to aggregate (e.g. "type", "tags").
            q: Optional search query to scope the aggregation.

        Returns:
            List of ``{"key", "doc_count"}`` buckets.
        """
        params: dict[str, Any] = {}
        if q:
            params["q"] = q

        try:
            response = await self._call_hub_api(
                "/api/search/v1/collections/all/aggregations", params=params
            )
        except RuntimeError as e:
            logger.warning(f"Hub Aggregations API error: {e}")
            return []

        data = response.json()
        logger.debug(f"Aggregations raw response: {data}")

        aggregations = data.get("aggregations", {})
        terms = aggregations.get("terms", []) if isinstance(aggregations, dict) else []

        for term_group in terms:
            if term_group.get("field") == field:
                raw_buckets = term_group.get("aggregations", [])
                return [
                    {"key": b.get("label", ""), "doc_count": b.get("value", 0)}
                    for b in raw_buckets
                ]

        return []

    # ── Health check ────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Check if the ArcGIS Hub API is accessible.

        Returns:
            True if healthy
        """
        try:
            await self._call_hub_api("/api/search/v1/collections")
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # ── Private helpers ─────────────────────────────────────────────────

    @staticmethod
    def _ensure_layer_url(service_url: str) -> str:
        """Append /0 if the URL points at a FeatureServer or MapServer root
        without a layer index (e.g. .../FeatureServer -> .../FeatureServer/0).
        """
        stripped = service_url.rstrip("/")
        if re.search(r"/(FeatureServer|MapServer)$", stripped, re.IGNORECASE):
            return f"{stripped}/0"
        return stripped

    @staticmethod
    def _epoch_ms_to_iso(epoch_ms: Any) -> str:
        if epoch_ms is None:
            return ""
        try:
            return datetime.fromtimestamp(int(epoch_ms) / 1000).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return ""

    @staticmethod
    def _extract_dataset_summary(props: dict[str, Any]) -> dict[str, Any]:
        description = props.get("description", "") or ""
        if len(description) > 300:
            description = description[:300] + "..."

        return {
            "id": props.get("id", ""),
            "title": props.get("title", ""),
            "description": description,
            "type": props.get("type", ""),
            "url": props.get("url", ""),
            "access": props.get("access", ""),
            "owner": props.get("owner", ""),
            "created": ArcGISPlugin._epoch_ms_to_iso(props.get("created")),
            "modified": ArcGISPlugin._epoch_ms_to_iso(props.get("modified")),
            "tags": props.get("tags", []),
            "extent": props.get("extent", []),
        }

    def _format_search_results(self, datasets: list[dict[str, Any]]) -> str:
        if not datasets:
            return "No datasets found."

        lines = [f"Found {len(datasets)} dataset(s):\n"]

        for i, ds in enumerate(datasets, 1):
            tags = ", ".join(ds.get("tags", [])) if ds.get("tags") else "None"
            lines.append(f"{i}. {ds.get('title', 'Untitled')}")
            lines.append(f"   ID: {ds.get('id', 'unknown')}")
            lines.append(f"   Type: {ds.get('type', 'unknown')}")
            lines.append(f"   Access: {ds.get('access', 'unknown')}")
            lines.append(f"   Description: {ds.get('description', 'No description')}")
            lines.append(f"   URL: {ds.get('url', '')}")
            lines.append(f"   Tags: {tags}")
            lines.append("")

        return "\n".join(lines)

    def _format_dataset(self, dataset: dict[str, Any]) -> str:
        tags = ", ".join(dataset.get("tags", [])) if dataset.get("tags") else "None"
        lines = [
            f"Dataset: {dataset.get('title', 'Untitled')}",
            f"ID: {dataset.get('id', 'unknown')}",
            f"Type: {dataset.get('type', 'unknown')}",
            f"Access: {dataset.get('access', 'unknown')}",
            f"Owner: {dataset.get('owner', 'unknown')}",
            f"Created: {dataset.get('created', '')}",
            f"Modified: {dataset.get('modified', '')}",
            f"Description: {dataset.get('description', 'No description')}",
            f"Snippet: {dataset.get('snippet', '')}",
            f"License: {dataset.get('licenseInfo', '')}",
            f"Spatial Reference: {dataset.get('spatialReference', '')}",
            f"Geometry Type: {dataset.get('geometryType', '')}",
            f"Number of Records: {dataset.get('numRecords', 'N/A')}",
            f"Tags: {tags}",
            f"Extent: {dataset.get('extent', [])}",
            f"Additional Resources: {dataset.get('additionalResources', [])}",
            f"URL: {dataset.get('url', '')}",
            f"Service URL (use for query_data): {dataset.get('service_url', '')}",
        ]
        return "\n".join(lines)

    def _format_schema(self, fields: list[dict[str, Any]]) -> str:
        """Format schema information for user display."""
        if not fields:
            return "No schema information available."

        lines = ["Schema fields:"]
        for field in fields:
            name = field.get("name", "unknown")
            ftype = field.get("type", "unknown")
            alias = field.get("alias", "")
            lines.append(f"  • {name} ({ftype})")
            if alias and alias != name:
                lines.append(f"    Alias: {alias}")

        return "\n".join(lines)

    def _format_query_results(self, records: list[dict[str, Any]], limit: int) -> str:
        if not records:
            return "No records returned."

        lines = [f"Returned {len(records)} record(s) (limit: {limit}):\n"]

        for i, record in enumerate(records, 1):
            lines.append(f"Record {i}:")
            for key, value in record.items():
                lines.append(f"  {key}: {value}")
            lines.append("")

        return "\n".join(lines)

    def _format_aggregations(self, field: str, buckets: list[dict[str, Any]]) -> str:
        if not buckets:
            return f"No aggregation results for '{field}'."

        lines = [f"Aggregations for '{field}':\n"]
        for bucket in buckets:
            lines.append(
                f"  {bucket.get('key', 'unknown')}: "
                f"{bucket.get('doc_count', bucket.get('count', 0))} dataset(s)"
            )

        return "\n".join(lines)