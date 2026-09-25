"""ArcGIS Hub plugin implementation for OpenContext.

This plugin provides access to ArcGIS Hub open data catalogs
via the OGC API - Records (Hub Search API) and ArcGIS Feature Services.
"""

import ipaddress
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from core.base_plugin import HTTP_RETRY, ROW_FORMATS, BaseOpenDataPlugin, ToolHandler
from core.interfaces import PluginType, ToolDefinition, ToolResult
from core.portal_content import join_cleaned
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.where_validator import WhereValidator

logger = logging.getLogger(__name__)

# Feature Service URL shape accepted for auto-trust: an ArcGIS REST services
# path ending in a FeatureServer/MapServer, optionally with a layer index.
_ARCGIS_SERVICE_PATH = re.compile(
    r"/rest/services/.+/(FeatureServer|MapServer)(/\d+)?/?$", re.IGNORECASE
)
# Hostname suffixes that are never valid public service hosts.
_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".localdomain")


class ArcGISPlugin(BaseOpenDataPlugin):
    """Plugin for accessing ArcGIS Hub open data catalogs.

    This plugin implements the DataPlugin interface on top of
    :class:`BaseOpenDataPlugin` and provides tools for searching datasets,
    retrieving dataset metadata, querying Feature Services, and exploring
    catalog aggregations.
    """

    plugin_name = "arcgis"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.1.0"

    config_class = ArcGISPluginConfig
    # Hub item IDs are 32-char hex; allow the underscore/hyphen variants seen
    # in layer references (e.g. abcdef..._0).
    id_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    provider_label = "ArcGIS Hub catalog"

    QUERYABLE_TYPES = {
        "Feature Layer",
        "Feature Service",
        "Map Service",
        "Table",
    }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        # service root URL -> resolved layer URL (see _resolve_layer_url)
        self._layer_url_cache: dict[str, str] = {}

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
            # Follow redirects (a renamed Hub domain keeps working) and drop
            # the bearer token if a hop leaves the trusted hosts. *.arcgis.com
            # and trusted_service_hosts stay trusted (feature services live
            # there); the same allow-list gates feature-service fetching.
            trusted = ("arcgis.com", *self.plugin_config.trusted_service_hosts)
            self.hub_client = self._create_http_client(
                base_url=self.plugin_config.portal_url,
                headers=headers,
                timeout=self.plugin_config.timeout,
                protect_headers=("Authorization",),
                trusted_hosts=trusted,
            )

            self.feature_client = self._create_http_client(
                headers=feature_headers,
                timeout=self.plugin_config.timeout,
                protect_headers=("Authorization",),
                trusted_hosts=trusted,
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
                        "query": {
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
                    "dataset has a queryable service URL. The reply gives the "
                    "total number of matching records and, when more remain, the "
                    "offset for the next page. Use format json or csv for rows "
                    "you will compute with, and order_by for stable paging."
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
                        "offset": {
                            "type": "integer",
                            "description": (
                                "Number of matching records to skip, for paging "
                                "(default: 0)"
                            ),
                            "default": 0,
                            "minimum": 0,
                        },
                        "order_by": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to sort by, each "
                                'optionally followed by ASC or DESC (e.g. "COUNTY, '
                                'UNITS DESC")'
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": list(ROW_FORMATS),
                            "description": (
                                "Output format: text (readable records, default), "
                                "json (array of objects), or csv (header plus one "
                                "line per record)"
                            ),
                            "default": "text",
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
            "search_datasets": ToolHandler(
                handler=self._tool_search_datasets,
                required_args=("query",),
                guidance=(
                    "Use the get_dataset tool with a dataset ID from the list "
                    "to get details, then get_schema and query_data."
                ),
            ),
            "get_dataset": ToolHandler(
                handler=self._tool_get_dataset,
                required_args=("dataset_id",),
                guidance=(
                    "Use the get_schema tool with this dataset's ID to list "
                    "fields, then query_data to query features."
                ),
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
        result = await self._search_hub(query, limit)
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": self._format_search_results(
                        result["results"], total=result["total"]
                    ),
                }
            ],
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
        query = arguments.get("query")
        buckets = await self.get_aggregations(field, query)
        return ToolResult(
            content=[
                {"type": "text", "text": self._format_aggregations(field, buckets)}
            ],
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
        offset = arguments.get("offset", 0)
        order_by = arguments.get("order_by")
        fmt = arguments.get("format") or "text"
        if fmt not in ROW_FORMATS:
            raise ValueError(
                f"format must be one of {', '.join(ROW_FORMATS)} (got {fmt!r})"
            )
        page = await self._query_page(
            dataset_id, where, out_fields, limit, offset=offset, order_by=order_by
        )
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": self._format_query_results(page, limit, fmt=fmt),
                }
            ],
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
        return (await self._search_hub(query, limit))["results"]

    async def _search_hub(self, query: str, limit: int) -> dict[str, Any]:
        """Search the Hub catalog; return ``{"results": [...], "total": int | None}``.

        ``total`` is the catalog-wide match count (``numberMatched``).
        """
        response = await self._call_hub_api(
            "/api/search/v1/collections/all/items",
            params={"q": query, "limit": limit},
        )

        data = response.json() or {}
        total = data.get("numberMatched")
        features = data.get("features", []) or []
        results = [
            self._extract_dataset_summary(feature.get("properties", {}) or {})
            for feature in features
        ]
        return {"results": results, "total": total if isinstance(total, int) else None}

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
                "numRecords": props.get("numRecords", props.get("recordCount")),
                "service_url": props.get("url", ""),
                "size": props.get("size"),
                "orgName": props.get("orgName", ""),
                "categories": props.get("categories", []),
                "typeKeywords": props.get("typeKeywords", []),
                "lastEditDate": self.short_date(
                    props.get("serviceLastEditDate") or props.get("lastEditDate")
                ),
                "accessInformation": props.get("accessInformation", ""),
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

        service_url = self._validate_feature_url(
            service_url,
            self.plugin_config.portal_url,
            self.plugin_config.trusted_service_hosts,
            auto_trust=self.plugin_config.auto_trust_hub_services,
        )
        layer_url = await self._resolve_layer_url(service_url)

        # The format must travel as a query parameter: httpx replaces the
        # URL's own query string with ``params``, so ``{url}?f=json`` plus
        # ``params={}`` sent a bare request and ArcGIS answered with an HTML
        # page ("Expecting value: line 1 column 1" on every dataset).
        response = await self._call_feature_service(layer_url, {"f": "json"})

        fields = self._fields_from_layer_metadata(response)
        if fields is None:
            # Some self-hosted services return HTML or a malformed body on
            # the layer metadata endpoint while the query endpoint works
            # (seen on Columbus and Indianapolis). Derive the schema from a
            # one-row query instead of failing.
            logger.warning(
                "Layer metadata at %s was not usable; deriving schema from a "
                "1-row query",
                layer_url,
            )
            fields = await self._schema_from_query(layer_url)
        return [
            {
                "name": f.get("name", ""),
                "type": f.get("type", ""),
                "alias": f.get("alias", ""),
            }
            for f in fields
        ]

    @staticmethod
    def _fields_from_layer_metadata(response: httpx.Response) -> list | None:
        """Extract ``fields`` from a layer metadata response, or ``None``.

        Returns ``None`` when the body is not JSON, carries an ArcGIS
        ``error`` envelope, or has no ``fields`` list, so the caller can
        fall back to deriving the schema from a query.
        """
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 - any parse failure triggers fallback
            return None
        if not isinstance(data, dict) or data.get("error"):
            return None
        fields = data.get("fields")
        return fields if isinstance(fields, list) else None

    async def _schema_from_query(self, service_url: str) -> list[dict[str, Any]]:
        """Derive a layer's field list from a one-row ``/query`` response.

        Args:
            service_url: Validated layer URL (``.../FeatureServer/<n>``).

        Returns:
            The ``fields`` list reported by the query endpoint.

        Raises:
            ValueError: If the query endpoint also fails to return usable JSON.
            RuntimeError: If the query endpoint reports an ArcGIS error.
        """
        params = {
            "where": "1=1",
            "outFields": "*",
            "resultRecordCount": 1,
            "returnGeometry": "false",
            "f": "json",
        }
        response = await self._call_feature_service(f"{service_url}/query", params)
        try:
            data = response.json()
        except Exception as json_err:
            content_type = response.headers.get("content-type", "")
            raise ValueError(
                "Feature Service returned non-JSON responses on both the layer "
                f"metadata and query endpoints (content-type: {content_type}); "
                "the dataset URL may not point to a queryable ArcGIS Feature "
                "Service."
            ) from json_err
        error_in_body = data.get("error") if isinstance(data, dict) else None
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            raise RuntimeError(
                f"Feature Service schema query failed (code {code}): {msg}"
            )
        fields = data.get("fields", []) if isinstance(data, dict) else []
        return fields if isinstance(fields, list) else []

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
        page = await self._query_page(
            dataset_id, where, out_fields, limit, with_count=False
        )
        return page["records"]

    async def _query_page(
        self,
        dataset_id: str,
        where: str,
        out_fields: str,
        limit: int,
        *,
        offset: int = 0,
        order_by: str | None = None,
        with_count: bool = True,
    ) -> dict[str, Any]:
        """Query one page of records, with the total match count.

        Resolves the Feature Service URL (two-hop), validates ``where`` and
        ``order_by``, and sends ``resultOffset``/``orderByFields`` when
        given. The total comes from a ``returnCountOnly`` request on the
        same WHERE clause; it is skipped when the page itself proves the
        total (a first page that came back short of ``limit`` with no
        transfer-limit flag).

        Args:
            dataset_id: Hub item ID.
            where: SQL WHERE clause (validated via :class:`WhereValidator`).
            out_fields: Comma-separated field names to return.
            limit: Maximum number of records (capped at 1000).
            offset: Matching records to skip.
            order_by: Sort order (validated via
                :meth:`WhereValidator.validate_order_by`).
            with_count: Whether to look up the total match count.

        Returns:
            ``{"records", "offset", "total", "exceeded"}``, where ``total`` is
            ``None`` when unknown and ``exceeded`` mirrors the service's
            ``exceededTransferLimit`` flag.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError(f"offset must be a non-negative integer (got {offset})")
        sort = WhereValidator.validate_order_by(order_by)

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
        service_url = self._validate_feature_url(
            service_url,
            self.plugin_config.portal_url,
            self.plugin_config.trusted_service_hosts,
            auto_trust=self.plugin_config.auto_trust_hub_services,
        )
        service_url = await self._resolve_layer_url(service_url)
        query_url = f"{service_url}/query"
        record_count = min(limit, 1000)
        params: dict[str, Any] = {
            "where": where_clause,
            "outFields": out_fields,
            "resultRecordCount": record_count,
            "f": "json",
            "returnGeometry": "false",
        }
        if offset:
            params["resultOffset"] = offset
        if sort:
            params["orderByFields"] = sort

        data = self._query_json(await self._call_feature_service(query_url, params))
        features = data.get("features") or []
        records = [f.get("attributes", {}) for f in features]
        exceeded = bool(data.get("exceededTransferLimit"))

        total: int | None = None
        if with_count:
            if offset == 0 and len(records) < record_count and not exceeded:
                total = len(records)
            else:
                total = await self._count_features(query_url, where_clause)

        return {
            "records": records,
            "offset": offset,
            "total": total,
            "exceeded": exceeded,
        }

    @staticmethod
    def _query_json(response: httpx.Response) -> dict[str, Any]:
        """Parse a Feature Service ``/query`` response, raising on errors.

        Raises:
            ValueError: If the body is not JSON.
            RuntimeError: If the body is an ArcGIS error envelope.
        """
        try:
            data = response.json()
        except Exception as json_err:
            content_type = response.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type}). The dataset URL may not "
                f"point to a queryable ArcGIS Feature Service."
            ) from json_err

        error_in_body = data.get("error") if isinstance(data, dict) else None
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            detail_str = "; ".join(details) if details else ""
            raise RuntimeError(
                f"Feature Service query failed (code {code}): {msg}"
                + (f" — {detail_str}" if detail_str else "")
            )
        return data if isinstance(data, dict) else {}

    async def _count_features(self, query_url: str, where: str) -> int | None:
        """Return how many records match ``where``, or ``None`` if unknown.

        A failed count does not fail the query; the page is still returned
        without a total.
        """
        params = {"where": where, "returnCountOnly": "true", "f": "json"}
        try:
            response = await self._call_feature_service(query_url, params)
            count = response.json().get("count")
        except Exception as exc:  # noqa: BLE001 - the count is best-effort
            logger.warning("Record count query failed for %s: %s", query_url, exc)
            return None
        if isinstance(count, bool) or not isinstance(count, int):
            return None
        return count

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
    def _validate_feature_url(
        service_url: str,
        portal_url: str,
        trusted_hosts: tuple[str, ...] | list[str] = (),
        *,
        auto_trust: bool = False,
    ) -> str:
        """Restrict Feature Service URLs to trusted hosts.

        Parses ``service_url`` and requires the scheme to be http/https and
        the host to end with ``.arcgis.com``, equal the configured portal
        host, or match one of ``trusted_hosts`` (exact host or subdomain,
        case-insensitive). This prevents a crafted dataset record from
        steering Feature Service queries to arbitrary hosts (SSRF). Ported
        from thealphacubicle/OpenContext (Feature/security update #37).

        Hub catalogs commonly reference services self-hosted on city
        domains (``gis.charlottenc.gov``, ``maps2.dcgis.dc.gov``, ...).
        With ``auto_trust`` (the ``auto_trust_hub_services`` config, on by
        default) such a host is accepted when the URL is https, the host is
        a public DNS name (not an IP literal, not a single label, not an
        internal suffix), and the path is an ArcGIS REST service path
        (``/rest/services/.../FeatureServer|MapServer[/<layer>]``). The
        plugin never sends its bearer token to auto-trusted hosts (see
        ``protect_headers`` in :meth:`initialize`). Operators who want a
        strict allow-list set ``auto_trust_hub_services: false`` and list
        hosts in ``trusted_service_hosts``.

        Args:
            service_url: Feature Service URL resolved from a dataset record.
            portal_url: Configured portal URL (its host is the allow-listed
                fallback for self-hosted ArcGIS portals).
            trusted_hosts: Extra hostnames from ``trusted_service_hosts``
                config.
            auto_trust: Accept Hub-referenced ArcGIS service URLs on hosts
                that are not explicitly listed (see above).

        Returns:
            The validated ``service_url`` (surrounding whitespace stripped).

        Raises:
            ValueError: If the scheme is not http/https or the host is not
                trusted. The message starts with ``untrusted_service_host:``
                and names the host so callers and agents can act on it.
        """
        service_url = (service_url or "").strip()
        parsed = urlparse(service_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Feature Service URL must use http or https (got: {parsed.scheme!r})"
            )
        host = (parsed.hostname or "").lower()
        portal_host = (urlparse(portal_url).hostname or "").lower()
        if not host:
            raise ValueError("Feature Service URL must include a hostname")
        if host == portal_host or host.endswith(".arcgis.com"):
            return service_url
        for trusted in trusted_hosts:
            trusted = trusted.lower().lstrip(".")
            if host == trusted or host.endswith(f".{trusted}"):
                return service_url

        if auto_trust:
            reason = ArcGISPlugin._auto_trust_refusal(parsed, host)
            if reason is None:
                logger.info(
                    "Auto-trusting Hub-referenced Feature Service host %r "
                    "(auto_trust_hub_services=true)",
                    host,
                )
                return service_url
            hint = f"auto-trust refused: {reason}; "
        else:
            hint = "auto_trust_hub_services is off; "

        raise ValueError(
            f"untrusted_service_host: {host!r} — Feature Service URL host is "
            f"not trusted ({hint}must end with '.arcgis.com', match portal host "
            f"{portal_host!r}, or be listed in trusted_service_hosts). Add "
            f"{host!r} to trusted_service_hosts in config.yaml to allow it."
        )

    @staticmethod
    def _auto_trust_refusal(parsed: Any, host: str) -> str | None:
        """Why a Hub-referenced service URL may not be auto-trusted, or None.

        Args:
            parsed: ``urlparse`` result for the service URL.
            host: Lowercased hostname from the URL.

        Returns:
            A short reason string when the URL fails the auto-trust rules,
            ``None`` when it qualifies.
        """
        if parsed.scheme != "https":
            return "only https URLs qualify"
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return "IP literals are never trusted"
        if "." not in host or host.endswith(_BLOCKED_HOST_SUFFIXES):
            return "host is not a public DNS name"
        if not _ARCGIS_SERVICE_PATH.search(parsed.path or ""):
            return "path is not an ArcGIS REST FeatureServer/MapServer path"
        return None

    async def _resolve_layer_url(self, service_url: str) -> str:
        """Return the layer URL to query for a service URL.

        A URL that already names a layer (``.../FeatureServer/4``) is
        returned as is. For a service root, the service description
        (``?f=json``) is read and the first layer's id is used, falling back
        to the first table, then to ``0``. Services whose only layer is not
        id 0 (HUD's "Low to Moderate Income Population by Tract" is layer 4,
        "Opportunity Zones" is layer 13) were unreachable when ``/0`` was
        assumed. Results are cached per service URL.

        Args:
            service_url: Validated Feature Service or layer URL.

        Returns:
            A layer URL (``.../FeatureServer/<id>``).
        """
        stripped = service_url.rstrip("/")
        if not re.search(r"/(FeatureServer|MapServer)$", stripped, re.IGNORECASE):
            return stripped

        cached = self._layer_url_cache.get(stripped)
        if cached is not None:
            return cached

        layer_id = 0
        try:
            response = await self._call_feature_service(stripped, {"f": "json"})
            data = response.json() or {}
            candidates = list(data.get("layers") or []) + list(data.get("tables") or [])
            first = next((c for c in candidates if isinstance(c.get("id"), int)), None)
            if first is not None:
                layer_id = int(first["id"])
        except Exception as exc:  # noqa: BLE001 - metadata unreachable: keep default
            logger.warning(
                "Could not read layer list for %s (%s); assuming layer 0", stripped, exc
            )

        resolved = f"{stripped}/{layer_id}"
        self._layer_url_cache[stripped] = resolved
        return resolved

    @staticmethod
    def _ensure_layer_url(service_url: str) -> str:
        """Append /0 if the URL points at a FeatureServer or MapServer root
        without a layer index (e.g. .../FeatureServer -> .../FeatureServer/0).
        Kept for callers that cannot await; :meth:`_resolve_layer_url` is
        preferred.
        """
        stripped = service_url.rstrip("/")
        if re.search(r"/(FeatureServer|MapServer)$", stripped, re.IGNORECASE):
            return f"{stripped}/0"
        return stripped

    @staticmethod
    def _epoch_ms_to_iso(epoch_ms: Any) -> str:
        """Render an ArcGIS epoch-millisecond timestamp as ``YYYY-MM-DD``."""
        return BaseOpenDataPlugin.short_date(epoch_ms)

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
            "recordCount": props.get("recordCount", props.get("numRecords")),
        }

    def _format_search_results(
        self, datasets: list[dict[str, Any]], *, total: int | None = None
    ) -> str:
        """Format Hub search hits.

        Args:
            datasets: Summaries from :meth:`_extract_dataset_summary`.
            total: Catalog-wide match count (``numberMatched``), if known.
        """
        if not datasets:
            return "No datasets found."

        lines = [self.format_search_header(total, len(datasets)), ""]

        for i, ds in enumerate(datasets, 1):
            tags = join_cleaned(ds.get("tags", [])) if ds.get("tags") else "None"
            lines.append(
                f"{i}. {self.portal_line(ds.get('title'), default='Untitled')}"
            )
            lines.append(f"   ID: {self.safe_id(ds.get('id'))}")
            lines.append(
                f"   Type: {self.portal_line(ds.get('type'), default='unknown')}"
            )
            lines.append(
                f"   Access: {self.portal_line(ds.get('access'), default='unknown')}"
            )
            facts = []
            owner = self.portal_line(ds.get("owner"))
            if owner:
                facts.append(f"Owner: {owner}")
            created = self.short_date(ds.get("created"))
            modified = self.short_date(ds.get("modified"))
            if created:
                facts.append(f"Created: {created}")
            if modified:
                facts.append(f"Modified: {modified}")
            records = ds.get("recordCount")
            if isinstance(records, int):
                facts.append(f"Records: {records}")
            if facts:
                lines.append("   " + " | ".join(facts))
            lines.append(
                f"   Description: {self.portal_line(ds.get('description'), max_len=300, default='No description')}"
            )
            lines.append(f"   URL: {self._display_url(ds.get('url'))}")
            lines.append(f"   Tags: {tags}")
            lines.append("")

        return "\n".join(lines)

    def _format_dataset(self, dataset: dict[str, Any]) -> str:
        """Format Hub item metadata; empty values are omitted."""
        line = self.portal_line
        lines = [
            f"Dataset: {line(dataset.get('title'), default='Untitled')}",
            f"ID: {self.safe_id(dataset.get('id'))}",
            f"Type: {line(dataset.get('type'), default='unknown')}",
            f"Access: {line(dataset.get('access'), default='unknown')}",
        ]

        def add(label: str, value: str) -> None:
            if value:
                lines.append(f"{label}: {value}")

        owner = line(dataset.get("owner"))
        org = line(dataset.get("orgName"))
        if owner or org:
            lines.append(
                "Owner: " + (f"{owner} ({org})" if owner and org else owner or org)
            )
        dates = []
        for label, key in (
            ("Created", "created"),
            ("Modified", "modified"),
            ("Last edit", "lastEditDate"),
        ):
            value = self.short_date(dataset.get(key))
            if value:
                dates.append(f"{label}: {value}")
        if dates:
            lines.append(" | ".join(dates))
        facts = []
        records = dataset.get("numRecords")
        if records is None:
            records = dataset.get("recordCount")
        if records is not None:
            facts.append(f"Records: {line(records)}")
        size = self.human_size(dataset.get("size"))
        if size:
            facts.append(f"Size: {size}")
        if facts:
            lines.append(" | ".join(facts))
        add("License", self.portal_block(dataset.get("licenseInfo"), max_len=1000))
        add("Access information", line(dataset.get("accessInformation")))
        add("Snippet", line(dataset.get("snippet")))
        lines.append(
            f"Description: {self.portal_block(dataset.get('description'), default='No description')}"
        )
        add("Spatial Reference", line(dataset.get("spatialReference")))
        add("Geometry Type", line(dataset.get("geometryType")))
        add("Tags", join_cleaned(dataset.get("tags") or []))
        add("Categories", join_cleaned(dataset.get("categories") or []))
        add("Type keywords", join_cleaned(dataset.get("typeKeywords") or []))
        extent = dataset.get("extent")
        if extent:
            add("Extent", line(extent))
        additional = dataset.get("additionalResources")
        if additional:
            add("Additional Resources", line(additional, max_len=1000))
        add("URL", self._display_url(dataset.get("url")))
        add("Service URL", self._display_url(dataset.get("service_url")))
        return "\n".join(lines)

    def _display_url(self, url: Any) -> str:
        """Render a portal-supplied URL only if its host is trusted.

        Delegates to :meth:`BaseOpenDataPlugin.display_portal_url` with the
        same allow-list that gates *fetching* (portal host, ``*.arcgis.com``,
        ``trusted_service_hosts``), so an attacker cannot plant a link to an
        arbitrary host in a dataset record.
        """
        return self.display_portal_url(
            url, extra_hosts=("arcgis.com", *self.plugin_config.trusted_service_hosts)
        )

    def _format_schema(self, fields: list[dict[str, Any]]) -> str:
        """Format schema information for user display."""
        if not fields:
            return "No schema information available."

        lines = ["Schema fields:"]
        for field in fields:
            name = self.portal_line(field.get("name"), default="unknown")
            ftype = self.portal_line(field.get("type"), default="unknown")
            alias = self.portal_line(field.get("alias"))
            lines.append(f"  • {name} ({ftype})")
            if alias and alias != name:
                lines.append(f"    Alias: {alias}")

        return "\n".join(lines)

    def _format_query_results(
        self, page: dict[str, Any], limit: int, *, fmt: str = "text"
    ) -> str:
        """Format a :meth:`_query_page` result with totals and paging hints.

        Args:
            page: ``{"records", "offset", "total", "exceeded"}``.
            limit: The requested ``limit``.
            fmt: Output format, one of :data:`ROW_FORMATS`.
        """
        records = page.get("records") or []
        offset = page.get("offset", 0)
        total = page.get("total")
        if not records:
            if total:
                return (
                    f"No records returned at offset {offset}; "
                    f"{total} record(s) match in total."
                )
            return "No records returned."

        window = f"(offset: {offset}, limit: {limit})"
        if total is not None:
            header = f"Returned {len(records)} of {total} matching record(s) {window}:"
        else:
            header = f"Returned {len(records)} record(s) {window}:"

        # ArcGIS records have no internal _id key to skip.
        text, shown = self.render_rows(
            records, fmt, header=header, skip_keys=frozenset()
        )
        next_offset = offset + shown
        if total is not None:
            more = next_offset < total
        else:
            more = shown < len(records) or bool(page.get("exceeded"))
        if more:
            text += (
                f"\n\nMore records match. Next page: offset={next_offset} "
                "with the same where, out_fields and order_by."
            )
        return text

    def _format_aggregations(self, field: str, buckets: list[dict[str, Any]]) -> str:
        if not buckets:
            return f"No aggregation results for '{field}'."

        lines = [f"Aggregations for '{field}':\n"]
        for bucket in buckets:
            lines.append(
                f"  {self.portal_line(bucket.get('key'), default='unknown')}: "
                f"{self.portal_line(bucket.get('doc_count', bucket.get('count', 0)))} dataset(s)"
            )

        return "\n".join(lines)
