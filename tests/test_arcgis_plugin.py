"""Comprehensive tests for ArcGIS Hub plugin.

These tests verify plugin initialization, tool execution, API interactions,
error handling, and data formatting. Tests are designed to fail if functionality breaks.
"""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pydantic import ValidationError

from core.interfaces import PluginType
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.plugin import ArcGISPlugin
from plugins.arcgis.where_validator import WhereValidator


@pytest.fixture
def arcgis_config():
    """Standard ArcGIS Hub plugin configuration."""
    return {
        "portal_url": "https://hub.arcgis.com",
        "city_name": "TestCity",
        "timeout": 120,
    }


def _mock_response(json_data, status_code=200, text=None, content_type=None):
    """Create a mock httpx response."""
    mock = Mock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status = Mock()
    mock.text = text if text is not None else ""
    mock.headers = Mock()
    mock.headers.get = Mock(return_value=content_type or "application/json")
    return mock


# ── Plugin attributes ──────────────────────────────────────────────────


class TestPluginAttributes:
    def test_plugin_attributes(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        assert plugin.plugin_name == "arcgis"
        assert plugin.plugin_type == PluginType.OPEN_DATA

    def test_config_built_eagerly_in_init(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        assert isinstance(plugin.plugin_config, ArcGISPluginConfig)
        assert plugin.plugin_config.city_name == "TestCity"

    def test_invalid_config_raises_in_init(self):
        with pytest.raises(ValidationError):
            ArcGISPlugin({"portal_url": "not-a-url", "city_name": "TestCity"})


# ── Initialization ─────────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_initialize_success(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                return_value=_mock_response({"features": []})
            )
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is True
            assert plugin._initialized is True
            assert plugin.hub_client is not None
            assert plugin.feature_client is not None

    @pytest.mark.asyncio
    async def test_initialize_creates_two_clients_via_helper(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                return_value=_mock_response({"features": []})
            )
            mock_client_class.return_value = mock_client

            await plugin.initialize()

            # Two clients (hub + feature) created and tracked by the base.
            assert mock_client_class.call_count == 2
            assert len(plugin._clients) == 2

    @pytest.mark.asyncio
    async def test_initialize_failure(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is False
            assert plugin._initialized is False

    @pytest.mark.asyncio
    async def test_initialize_includes_token_header(self, arcgis_config):
        arcgis_config["token"] = "test-token-123"
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                return_value=_mock_response({"features": []})
            )
            mock_client_class.return_value = mock_client

            await plugin.initialize()

            # Both clients should carry the Authorization header.
            for call in mock_client_class.call_args_list:
                call_kwargs = call[1]
                assert call_kwargs["headers"]["Authorization"] == "Bearer test-token-123"

    @pytest.mark.asyncio
    async def test_shutdown_closes_tracked_clients(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                return_value=_mock_response({"features": []})
            )
            mock_client_class.return_value = mock_client

            await plugin.initialize()
            await plugin.shutdown()

            # Base shutdown closes all tracked clients and clears the list.
            assert mock_client.aclose.call_count == 2
            assert plugin._clients == []
            assert plugin._initialized is False


# ── get_tools ──────────────────────────────────────────────────────────


class TestGetTools:
    def test_get_tools_returns_five_tools(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        tools = plugin.get_tools()

        assert len(tools) == 5
        tool_names = [t.name for t in tools]
        assert "search_datasets" in tool_names
        assert "get_dataset" in tool_names
        assert "get_aggregations" in tool_names
        assert "get_schema" in tool_names
        assert "query_data" in tool_names

    def test_get_tools_uses_city_name_directly(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        tools = plugin.get_tools()
        search_tool = next(t for t in tools if t.name == "search_datasets")
        assert "TestCity" in search_tool.description

    def test_search_datasets_param_renamed_to_query(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        tools = plugin.get_tools()
        search_tool = next(t for t in tools if t.name == "search_datasets")
        assert "query" in search_tool.input_schema["properties"]
        assert "q" not in search_tool.input_schema["properties"]
        assert search_tool.input_schema["required"] == ["query"]

    def test_get_schema_tool_definition(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        tools = plugin.get_tools()
        schema_tool = next(t for t in tools if t.name == "get_schema")
        assert schema_tool.input_schema["required"] == ["dataset_id"]


# ── execute_tool dispatch ─────────────────────────────────────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        result = await plugin.execute_tool("unknown_tool", {})

        assert result.success is False
        assert "Unknown tool" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_tool_search_datasets_uses_query_param(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "search_datasets",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "abc123",
                    "title": "Test Dataset",
                    "tags": [],
                    "description": "desc",
                }
            ],
        ) as mock_search:
            result = await plugin.execute_tool("search_datasets", {"query": "test"})

        assert result.success is True
        assert len(result.content) > 0
        assert "text" in result.content[0]
        mock_search.assert_called_once_with("test", 10)

    @pytest.mark.asyncio
    async def test_execute_tool_search_datasets_rejects_old_q_param(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "search_datasets",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_search:
            # 'q' is no longer a recognized param; query defaults to "".
            result = await plugin.execute_tool("search_datasets", {"q": "test"})

        assert result.success is True
        mock_search.assert_called_once_with("", 10)

    @pytest.mark.asyncio
    async def test_execute_tool_get_dataset_missing_id(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        result = await plugin.execute_tool("get_dataset", {})

        assert result.success is False
        assert "dataset_id is required" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_tool_get_dataset(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Test",
                "tags": [],
                "description": "desc",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer/0",
            },
        ):
            result = await plugin.execute_tool("get_dataset", {"dataset_id": "abc123"})

        assert result.success is True
        assert len(result.content) > 0

    @pytest.mark.asyncio
    async def test_execute_tool_query_data_passes_where_out_fields(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "_query_features",
            new_callable=AsyncMock,
            return_value=[{"name": "Park A", "status": "Open"}],
        ) as mock_qf:
            result = await plugin.execute_tool(
                "query_data",
                {
                    "dataset_id": "abc123",
                    "where": "status = 'Open'",
                    "out_fields": "name,status",
                    "limit": 50,
                },
            )

        assert result.success is True
        assert len(result.content) > 0
        mock_qf.assert_called_once_with("abc123", "status = 'Open'", "name,status", 50)

    @pytest.mark.asyncio
    async def test_execute_tool_query_data_defaults(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "_query_features",
            new_callable=AsyncMock,
            return_value=[{"name": "Park A"}],
        ) as mock_qf:
            result = await plugin.execute_tool("query_data", {"dataset_id": "abc123"})

        assert result.success is True
        mock_qf.assert_called_once_with("abc123", "1=1", "*", 100)

    @pytest.mark.asyncio
    async def test_execute_tool_get_aggregations(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "get_aggregations",
            new_callable=AsyncMock,
            return_value=[
                {"key": "Feature Layer", "doc_count": 42},
                {"key": "Table", "doc_count": 10},
            ],
        ):
            result = await plugin.execute_tool("get_aggregations", {"field": "type"})

        assert result.success is True
        assert "Feature Layer" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_execute_tool_get_aggregations_missing_field(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        result = await plugin.execute_tool("get_aggregations", {})

        assert result.success is False
        assert "field is required" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_tool_get_schema(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "get_schema",
            new_callable=AsyncMock,
            return_value=[
                {"name": "name", "type": "esriFieldTypeString", "alias": "Name"},
            ],
        ):
            result = await plugin.execute_tool("get_schema", {"dataset_id": "abc123"})

        assert result.success is True
        assert "name" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_execute_tool_get_schema_missing_dataset_id(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        result = await plugin.execute_tool("get_schema", {})

        assert result.success is False
        assert "dataset_id is required" in result.error_message


# ── search_datasets / get_dataset (Hub API) ──────────────────────────


class TestHubApiMethods:
    @pytest.mark.asyncio
    async def test_search_datasets_parses_features(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            return_value=_mock_response(
                {
                    "features": [
                        {
                            "properties": {
                                "id": "abc123",
                                "title": "Parks",
                                "type": "Feature Layer",
                                "tags": ["parks"],
                                "description": "desc",
                            }
                        }
                    ]
                }
            )
        )

        results = await plugin.search_datasets("parks", 10)
        assert len(results) == 1
        assert results[0]["id"] == "abc123"
        assert results[0]["title"] == "Parks"

    @pytest.mark.asyncio
    async def test_search_datasets_empty(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            return_value=_mock_response({"features": []})
        )

        results = await plugin.search_datasets("nothing", 10)
        assert results == []

    @pytest.mark.asyncio
    async def test_get_dataset_returns_service_url(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            return_value=_mock_response(
                {
                    "properties": {
                        "id": "abc123",
                        "title": "Parks",
                        "url": "https://services.arcgis.com/xyz/FeatureServer/0",
                        "type": "Feature Layer",
                    }
                }
            )
        )

        dataset = await plugin.get_dataset("abc123")
        assert dataset["service_url"] == "https://services.arcgis.com/xyz/FeatureServer/0"
        assert dataset["snippet"] == ""


# ── get_schema (Feature Service metadata) ────────────────────────────


class TestGetSchema:
    @pytest.mark.asyncio
    async def test_get_schema_returns_fields(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer/0",
            },
        ):
            plugin.feature_client = AsyncMock()
            plugin.feature_client.get = AsyncMock(
                return_value=_mock_response(
                    {
                        "fields": [
                            {
                                "name": "name",
                                "type": "esriFieldTypeString",
                                "alias": "Name",
                            },
                            {
                                "name": "status",
                                "type": "esriFieldTypeString",
                                "alias": "Status",
                            },
                        ]
                    }
                )
            )

            schema = await plugin.get_schema("abc123")

        assert len(schema) == 2
        assert schema[0]["name"] == "name"
        assert schema[0]["type"] == "esriFieldTypeString"
        assert schema[0]["alias"] == "Name"

    @pytest.mark.asyncio
    async def test_get_schema_appends_layer_index(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer",
            },
        ):
            plugin.feature_client = AsyncMock()
            plugin.feature_client.get = AsyncMock(
                return_value=_mock_response({"fields": []})
            )

            await plugin.get_schema("abc123")

        url_called = plugin.feature_client.get.call_args[0][0]
        assert "/FeatureServer/0?f=json" in url_called

    @pytest.mark.asyncio
    async def test_get_schema_no_service_url_raises(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={"id": "abc123", "service_url": ""},
        ), pytest.raises(ValueError, match="does not have a queryable Feature Service URL"):
            await plugin.get_schema("abc123")


# ── query_data (DataPlugin contract) ─────────────────────────────────


class TestQueryDataContract:
    @pytest.mark.asyncio
    async def test_query_data_compiles_filters_to_where(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "_query_features",
            new_callable=AsyncMock,
            return_value=[{"name": "Park A"}],
        ) as mock_qf:
            await plugin.query_data(
                "abc123",
                filters={"status": "Open", "year": 2020},
                limit=50,
            )

        call_args = mock_qf.call_args
        assert call_args[0][0] == "abc123"
        where = call_args[0][1]
        assert "status = 'Open'" in where
        assert "year = 2020" in where
        assert call_args[0][2] == "*"
        assert call_args[0][3] == 50

    @pytest.mark.asyncio
    async def test_query_data_no_filters_defaults_to_1_equals_1(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "_query_features",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_qf:
            await plugin.query_data("abc123", filters=None, limit=100)

        assert mock_qf.call_args[0][1] == "1=1"

    @pytest.mark.asyncio
    async def test_query_data_none_filter_becomes_is_null(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "_query_features",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_qf:
            await plugin.query_data("abc123", filters={"status": None}, limit=10)

        assert "status IS NULL" in mock_qf.call_args[0][1]

    @pytest.mark.asyncio
    async def test_query_data_rejects_forbidden_field_name(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with pytest.raises(ValueError, match="Invalid field name"):
            await plugin.query_data("abc123", filters={"DELETE": "x"}, limit=10)


# ── _query_features (Feature Service two-hop) ────────────────────────


class TestQueryFeaturesTwoHop:
    @pytest.mark.asyncio
    async def test_query_features_two_hop(self, arcgis_config):
        """Verify _query_features calls get_dataset first, then the Feature Service."""
        plugin = ArcGISPlugin(arcgis_config)

        mock_feature_client = AsyncMock()
        mock_feature_client.get = AsyncMock(
            return_value=_mock_response(
                {
                    "features": [
                        {"attributes": {"name": "Park A", "status": "Open"}},
                    ]
                }
            )
        )
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "type": "Feature Layer",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer/0",
            },
        ) as mock_get_dataset:
            records = await plugin._query_features("abc123", "1=1", "*", 100)

        mock_get_dataset.assert_called_once_with("abc123")
        mock_feature_client.get.assert_called_once()
        call_args = mock_feature_client.get.call_args
        assert "/query" in call_args[0][0]
        assert len(records) == 1
        assert records[0]["name"] == "Park A"

    @pytest.mark.asyncio
    async def test_query_features_auto_appends_layer_index(self, arcgis_config):
        """When service_url ends with /FeatureServer (no layer), /0 is appended."""
        plugin = ArcGISPlugin(arcgis_config)

        mock_feature_client = AsyncMock()
        mock_feature_client.get = AsyncMock(
            return_value=_mock_response(
                {"features": [{"attributes": {"name": "Skate Park"}}]}
            )
        )
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "type": "Feature Layer",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer",
            },
        ):
            records = await plugin._query_features("abc123", "1=1", "*", 100)

        url_called = mock_feature_client.get.call_args[0][0]
        assert "/FeatureServer/0/query" in url_called
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_query_features_no_service_url_raises(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={"id": "abc123", "type": "Feature Layer", "service_url": ""},
        ), pytest.raises(ValueError, match="does not have a queryable Feature Service URL"):
            await plugin._query_features("abc123", "1=1", "*", 100)

    @pytest.mark.asyncio
    async def test_query_features_non_queryable_type_raises(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "type": "Image Service",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer/0",
            },
        ), pytest.raises(ValueError, match="not queryable"):
            await plugin._query_features("abc123", "1=1", "*", 100)

    @pytest.mark.asyncio
    async def test_query_features_retries_on_transient_error(self, arcgis_config):
        """HTTP_RETRY retries transient (non-HTTPStatusError) failures.

        httpx.ConnectError is not in the no-retry list, so it is retried.
        """
        plugin = ArcGISPlugin(arcgis_config)

        good_response = _mock_response({"features": [{"attributes": {"name": "ok"}}]})

        mock_feature_client = AsyncMock()
        mock_feature_client.get = AsyncMock(
            side_effect=[httpx.ConnectError("transient"), good_response]
        )
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "type": "Feature Layer",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer/0",
            },
        ):
            records = await plugin._query_features("abc123", "1=1", "*", 100)

        assert len(records) == 1
        assert records[0]["name"] == "ok"

    @pytest.mark.asyncio
    async def test_query_features_feature_error_in_body_raises(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        mock_feature_client = AsyncMock()
        mock_feature_client.get = AsyncMock(
            return_value=_mock_response(
                {"error": {"code": 400, "message": "Invalid query"}}
            )
        )
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "type": "Feature Layer",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer/0",
            },
        ), pytest.raises(RuntimeError, match="Feature Service query failed"):
            await plugin._query_features("abc123", "1=1", "*", 100)


# ── Layer URL helper ───────────────────────────────────────────────────


class TestEnsureLayerUrl:
    def test_appends_layer_to_feature_server_root(self):
        result = ArcGISPlugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/0"

    def test_preserves_existing_layer_index(self):
        result = ArcGISPlugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer/3"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/3"

    def test_handles_map_server(self):
        result = ArcGISPlugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/MapServer"
        )
        assert result == "https://services.arcgis.com/xyz/MapServer/0"

    def test_strips_trailing_slash(self):
        result = ArcGISPlugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer/"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/0"


# ── WhereValidator ────────────────────────────────────────────────────


class TestWhereValidator:
    def test_where_validator_blocks_delete(self):
        with pytest.raises(ValueError, match="DELETE"):
            WhereValidator.validate("DELETE FROM x")

    def test_where_validator_allows_valid(self):
        result = WhereValidator.validate("status = 'Active'")
        assert result == "status = 'Active'"

    def test_where_validator_empty(self):
        result = WhereValidator.validate("")
        assert result == "1=1"

    def test_where_validator_does_not_flag_deleted_at(self):
        result = WhereValidator.validate("deleted_at IS NULL")
        assert result == "deleted_at IS NULL"

    def test_where_validator_blocks_grant(self):
        """ArcGIS now gains GRANT (previously missing) via the base scan."""
        with pytest.raises(ValueError, match="GRANT"):
            WhereValidator.validate("GRANT SELECT ON x TO y")

    def test_where_validator_blocks_revoke(self):
        with pytest.raises(ValueError, match="REVOKE"):
            WhereValidator.validate("REVOKE SELECT ON x FROM y")

    def test_where_validator_blocks_declare(self):
        with pytest.raises(ValueError, match="DECLARE"):
            WhereValidator.validate("DECLARE @x INT")

    def test_where_validator_blocks_set(self):
        with pytest.raises(ValueError, match="SET"):
            WhereValidator.validate("SET role admin")


# ── Config schema ──────────────────────────────────────────────────────


class TestConfigSchema:
    def test_config_schema_valid(self):
        config = ArcGISPluginConfig(
            portal_url="https://hub.arcgis.com",
            city_name="Boston",
            timeout=60,
        )
        assert config.city_name == "Boston"
        assert config.portal_url == "https://hub.arcgis.com"
        assert config.timeout == 60
        assert config.token is None

    def test_config_schema_defaults(self):
        config = ArcGISPluginConfig(city_name="Boston")
        assert config.portal_url == "https://hub.arcgis.com"
        assert config.timeout == 120.0
        assert config.enabled is False
        assert config.token is None

    def test_config_schema_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(
                portal_url="https://hub.arcgis.com",
                city_name="Boston",
                unknown_field="oops",
            )

    def test_config_schema_strips_trailing_slash(self):
        config = ArcGISPluginConfig(
            portal_url="https://hub.arcgis.com/",
            city_name="Boston",
        )
        assert config.portal_url == "https://hub.arcgis.com"

    def test_config_schema_rejects_invalid_url(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(
                portal_url="not-a-url",
                city_name="Boston",
            )


# ── Health check ─────────────────────────────────────────────────────


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_succeeds(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            return_value=_mock_response({"features": []})
        )

        health = await plugin.health_check()
        assert health is True

    @pytest.mark.asyncio
    async def test_health_check_fails_on_http_error(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection failed")
        )

        health = await plugin.health_check()
        assert health is False

    @pytest.mark.asyncio
    async def test_health_check_fails_on_status_error(self, arcgis_config):
        """health_check uses raise_for_status (via _call_hub_api), not status_code."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error",
                request=httpx.Request("GET", "https://hub.arcgis.com/x"),
                response=httpx.Response(500, text="Server Error"),
            )
        )

        health = await plugin.health_check()
        assert health is False


# ── Aggregations ──────────────────────────────────────────────────────


class TestAggregations:
    @pytest.mark.asyncio
    async def test_get_aggregations_returns_buckets(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            return_value=_mock_response(
                {
                    "aggregations": {
                        "terms": [
                            {
                                "field": "type",
                                "aggregations": [
                                    {"label": "Feature Layer", "value": 42},
                                    {"label": "Table", "value": 10},
                                ],
                            }
                        ]
                    }
                }
            )
        )

        buckets = await plugin.get_aggregations("type")
        assert len(buckets) == 2
        assert buckets[0]["key"] == "Feature Layer"
        assert buckets[0]["doc_count"] == 42

    @pytest.mark.asyncio
    async def test_get_aggregations_no_match_returns_empty(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            return_value=_mock_response(
                {
                    "aggregations": {
                        "terms": [
                            {
                                "field": "tags",
                                "aggregations": [],
                            }
                        ]
                    }
                }
            )
        )

        buckets = await plugin.get_aggregations("type")
        assert buckets == []

    @pytest.mark.asyncio
    async def test_get_aggregations_swallows_runtime_error(self, arcgis_config):
        """get_aggregations returns [] on HTTP errors (best-effort helper)."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.hub_client = AsyncMock()
        plugin.hub_client.get = AsyncMock(
            side_effect=RuntimeError("HTTP error")
        )

        buckets = await plugin.get_aggregations("type")
        assert buckets == []