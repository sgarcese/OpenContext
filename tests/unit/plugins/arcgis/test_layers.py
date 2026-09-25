"""Tests for the ArcGIS ``layer`` argument and layer listing (#30).

Modelled on HUD's Small Area Fair Market Rents service: layer 0 holds ZIP
code geometry only, and the rents live in table 1.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from plugins.arcgis.plugin import ArcGISPlugin

FS = "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/SAFMR/FeatureServer"
SERVICE_INFO = {
    "layers": [
        {
            "id": 0,
            "name": "SAFMR_Zip_Code_Tab_Areas",
            "geometryType": "esriGeometryPolygon",
        }
    ],
    "tables": [{"id": 1, "name": "SAFMR_table"}],
}


def _response(data):
    mock = Mock()
    mock.json.return_value = data
    mock.raise_for_status = Mock()
    mock.headers = Mock()
    mock.headers.get = Mock(return_value="application/json")
    return mock


@pytest.fixture
def plugin():
    p = ArcGISPlugin(
        {"portal_url": "https://hub.arcgis.com", "city_name": "HUD", "timeout": 30}
    )
    p.feature_client = AsyncMock()
    return p


def _serve(plugin, *payloads):
    plugin.feature_client.get = AsyncMock(side_effect=[_response(p) for p in payloads])


def _urls(plugin):
    return [c.args[0] for c in plugin.feature_client.get.call_args_list]


def _dataset(service_url=FS):
    return {
        "id": "6458c67bad2a4cc7aa97514ef7ba8a0e",
        "title": "Small Area Fair Market Rents",
        "type": "Feature Service",
        "service_url": service_url,
    }


class TestResolveLayerUrl:
    async def test_explicit_layer_reaches_a_table(self, plugin):
        _serve(plugin, SERVICE_INFO)
        assert await plugin._resolve_layer_url(FS, 1) == f"{FS}/1"
        assert _urls(plugin) == [FS]

    async def test_explicit_layer_replaces_the_items_layer(self, plugin):
        _serve(plugin, SERVICE_INFO)
        assert await plugin._resolve_layer_url(f"{FS}/0", 1) == f"{FS}/1"

    async def test_unknown_layer_lists_what_exists(self, plugin):
        _serve(plugin, SERVICE_INFO)
        with pytest.raises(ValueError) as exc:
            await plugin._resolve_layer_url(FS, 5)
        message = str(exc.value)
        assert "layer 5 is not in this service" in message
        assert "0: SAFMR_Zip_Code_Tab_Areas (polygon layer)" in message
        assert "1: SAFMR_table (table)" in message

    @pytest.mark.parametrize("bad", [-1, True, "1", 1.0])
    async def test_layer_must_be_a_non_negative_integer(self, plugin, bad):
        with pytest.raises(ValueError, match="non-negative integer"):
            await plugin._resolve_layer_url(FS, bad)
        plugin.feature_client.get.assert_not_called()

    async def test_layer_needs_a_service_url(self, plugin):
        with pytest.raises(ValueError, match="FeatureServer or MapServer"):
            await plugin._resolve_layer_url("https://services.arcgis.com/x/query", 1)

    async def test_unreadable_layer_list_lets_the_service_decide(self, plugin):
        plugin.feature_client.get = AsyncMock(side_effect=RuntimeError("boom"))
        assert await plugin._resolve_layer_url(FS, 3) == f"{FS}/3"

    async def test_layer_list_is_fetched_once(self, plugin):
        _serve(plugin, SERVICE_INFO)
        await plugin._resolve_layer_url(FS, 1)
        await plugin._resolve_layer_url(FS, 0)
        await plugin._resolve_layer_url(FS)
        assert plugin.feature_client.get.call_count == 1

    async def test_default_uses_first_layer_before_tables(self, plugin):
        _serve(plugin, SERVICE_INFO)
        assert await plugin._resolve_layer_url(FS) == f"{FS}/0"


class TestToolsPassLayer:
    async def test_query_data_queries_the_requested_table(self, plugin):
        rows = {"features": [{"attributes": {"ZIP": "46601", "SAFMR_2BR": 1060}}]}
        _serve(plugin, SERVICE_INFO, rows)
        with patch.object(
            plugin, "get_dataset", new_callable=AsyncMock, return_value=_dataset()
        ):
            result = await plugin.execute_tool(
                "query_data",
                {
                    "dataset_id": "6458c67bad2a4cc7aa97514ef7ba8a0e",
                    "layer": 1,
                    "where": "ZIP='46601'",
                },
            )
        assert result.success is True
        assert _urls(plugin)[1] == f"{FS}/1/query"
        assert "SAFMR_2BR: 1060" in result.content[0]["text"]

    async def test_get_schema_reads_the_requested_table(self, plugin):
        _serve(plugin, SERVICE_INFO, {"fields": [{"name": "SAFMR_2BR"}]})
        with patch.object(
            plugin, "get_dataset", new_callable=AsyncMock, return_value=_dataset()
        ):
            result = await plugin.execute_tool(
                "get_schema", {"dataset_id": "abc", "layer": 1}
            )
        assert _urls(plugin)[1] == f"{FS}/1"
        assert "SAFMR_2BR" in result.content[0]["text"]

    async def test_bad_layer_is_refused_before_querying(self, plugin):
        _serve(plugin, SERVICE_INFO)
        with patch.object(
            plugin, "get_dataset", new_callable=AsyncMock, return_value=_dataset()
        ):
            result = await plugin.execute_tool(
                "query_data", {"dataset_id": "abc", "layer": "1; DROP TABLE t"}
            )
        assert result.success is False
        assert "non-negative integer" in result.error_message
        plugin.feature_client.get.assert_not_called()


class TestGetDatasetListsLayers:
    async def _get_dataset_text(self, plugin, dataset):
        with patch.object(
            plugin, "get_dataset", new_callable=AsyncMock, return_value=dataset
        ):
            result = await plugin.execute_tool("get_dataset", {"dataset_id": "abc"})
        assert result.success is True
        return result.content[0]["text"]

    async def test_lists_layers_and_tables_with_the_default(self, plugin):
        _serve(plugin, SERVICE_INFO)
        text = await self._get_dataset_text(plugin, _dataset())
        assert "Layers and tables (pass the id as layer" in text
        assert "  0: SAFMR_Zip_Code_Tab_Areas (polygon layer) [default]" in text
        assert "  1: SAFMR_table (table)" in text
        assert "  1: SAFMR_table (table) [default]" not in text

    async def test_default_follows_the_items_layer(self, plugin):
        _serve(plugin, SERVICE_INFO)
        text = await self._get_dataset_text(plugin, _dataset(f"{FS}/1"))
        assert "  1: SAFMR_table (table) [default]" in text

    async def test_unreadable_service_omits_the_section(self, plugin):
        plugin.feature_client.get = AsyncMock(side_effect=RuntimeError("boom"))
        text = await self._get_dataset_text(plugin, _dataset())
        assert "Layers and tables" not in text

    async def test_untrusted_host_is_never_contacted(self, plugin):
        untrusted = "http://10.0.0.5/arcgis/rest/services/X/FeatureServer"
        text = await self._get_dataset_text(plugin, _dataset(untrusted))
        assert "Layers and tables" not in text
        plugin.feature_client.get.assert_not_called()

    async def test_non_service_item_skips_the_lookup(self, plugin):
        text = await self._get_dataset_text(plugin, _dataset(""))
        assert "Layers and tables" not in text
        plugin.feature_client.get.assert_not_called()
