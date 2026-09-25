"""Tests for ArcGIS metadata: descriptions, edit dates, domains, source block (#32)."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from plugins.arcgis.plugin import DESCRIPTION_MAX, ArcGISPlugin

FS = "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/FMR/FeatureServer"
ITEM_ID = "12d2516901f947b5bb4da4e780e35f07"
# HUD's Fair Market Rents layer: editingInfo.dataLastEditDate 1759276162016.
LAYER_METADATA = {
    "fields": [
        {"name": "OBJECTID", "type": "esriFieldTypeOID"},
        {"name": "FMR_2BR", "type": "esriFieldTypeDouble"},
    ],
    "editingInfo": {
        "lastEditDate": 1759276162016,
        "schemaLastEditDate": 1727740800000,
        "dataLastEditDate": 1759276162016,
    },
}
SERVICE_INFO = {
    "layers": [{"id": 0, "name": "FMR", "geometryType": "esriGeometryPolygon"}]
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
        {
            "portal_url": "https://hudgis-hud.opendata.arcgis.com",
            "city_name": "HUD",
            "timeout": 30,
        }
    )
    p.feature_client = AsyncMock()
    return p


def _serve(plugin, *payloads):
    plugin.feature_client.get = AsyncMock(side_effect=[_response(p) for p in payloads])


def _dataset(**extra):
    return {
        "id": ITEM_ID,
        "title": "Fair Market Rents",
        "type": "Feature Service",
        "service_url": FS,
        **extra,
    }


def _body(result):
    assert result.success is True, result.error_message
    return result.content[0]["text"]


async def _tool(plugin, name, arguments, dataset):
    with patch.object(
        plugin, "get_dataset", new_callable=AsyncMock, return_value=dataset
    ):
        return await plugin.execute_tool(name, {"dataset_id": ITEM_ID, **arguments})


class TestDescription:
    async def test_full_description_as_plain_text(self, plugin):
        paragraph = "<p>" + "Fair Market Rents are gross rent estimates. " * 80 + "</p>"
        html_description = paragraph + "<ul><li>-4 means suppressed</li></ul>"
        summary = ArcGISPlugin._extract_dataset_summary(
            {"description": html_description}
        )
        _serve(plugin, SERVICE_INFO, LAYER_METADATA)
        text = _body(await _tool(plugin, "get_dataset", {}, _dataset(**summary)))
        assert "<p>" not in text
        assert "- -4 means suppressed" in text
        assert "…[truncated" not in text
        assert text.count("gross rent estimates") == 80

    async def test_description_past_the_budget_is_marked(self, plugin):
        dataset = _dataset(description="x" * (DESCRIPTION_MAX + 500))
        _serve(plugin, SERVICE_INFO, LAYER_METADATA)
        text = _body(await _tool(plugin, "get_dataset", {}, dataset))
        assert "…[truncated, 500 more chars]" in text

    def test_search_results_keep_a_short_excerpt(self, plugin):
        hits = [{"id": ITEM_ID, "title": "FMR", "description": "y" * 1_000}]
        text = plugin._format_search_results(hits)
        description_line = next(
            line for line in text.splitlines() if "Description:" in line
        )
        assert len(description_line) < 400

    async def test_license_html_becomes_text(self, plugin):
        with patch.object(plugin, "_call_hub_api", new_callable=AsyncMock) as hub:
            hub.return_value = _response(
                {
                    "properties": {
                        "id": ITEM_ID,
                        "licenseInfo": "<p>Public domain. See <a href='x'>terms</a>.</p>",
                        "accessInformation": "<div>HUD PD&amp;R</div>",
                    }
                }
            )
            dataset = await plugin.get_dataset(ITEM_ID)
        assert dataset["licenseInfo"] == "Public domain. See terms."
        assert dataset["accessInformation"] == "HUD PD&R"


class TestEditDates:
    async def test_get_dataset_shows_layer_edit_dates(self, plugin):
        _serve(plugin, SERVICE_INFO, LAYER_METADATA)
        text = _body(await _tool(plugin, "get_dataset", {}, _dataset()))
        assert "Data last edited: 2025-09-30" in text
        assert "Schema last edited: 2024-10-01" in text
        urls = [c.args[0] for c in plugin.feature_client.get.call_args_list]
        assert urls == [FS, f"{FS}/0"]

    async def test_unreadable_layer_metadata_omits_dates(self, plugin):
        plugin.feature_client.get = AsyncMock(
            side_effect=[_response(SERVICE_INFO), RuntimeError("boom")]
        )
        text = _body(await _tool(plugin, "get_dataset", {}, _dataset()))
        assert "Data last edited" not in text
        assert "0: FMR (polygon layer) [default]" in text


class TestSourceBlock:
    async def test_query_after_get_dataset_cites_the_data_date(self, plugin):
        rows = {"features": [{"attributes": {"FMR_2BR": 1060}}]}
        _serve(plugin, SERVICE_INFO, LAYER_METADATA, rows)
        await _tool(plugin, "get_dataset", {}, _dataset())
        text = _body(await _tool(plugin, "query_data", {}, _dataset()))
        assert "Source:" in text
        assert (
            f"  Dataset: Fair Market Rents "
            f"(https://hudgis-hud.opendata.arcgis.com/datasets/{ITEM_ID})"
        ) in text
        assert f"  Layer queried: {FS}/0" in text
        assert "  Data last edited: 2025-09-30" in text
        assert "  Retrieved: " in text and " UTC" in text
        # get_dataset: service + layer metadata; query_data: one query only.
        assert plugin.feature_client.get.call_count == 3

    async def test_query_without_cached_metadata_costs_no_extra_request(self, plugin):
        rows = {"features": [{"attributes": {"FMR_2BR": 1060}}]}
        _serve(plugin, SERVICE_INFO, rows)
        text = _body(await _tool(plugin, "query_data", {}, _dataset()))
        assert "Source:" in text
        assert "Data last edited" not in text
        assert plugin.feature_client.get.call_count == 2

    async def test_aggregate_cites_the_data_date(self, plugin):
        groups = {"features": [{"attributes": {"n": 5}}]}
        _serve(plugin, SERVICE_INFO, LAYER_METADATA, groups)
        text = _body(
            await _tool(
                plugin,
                "aggregate_data",
                {"statistics": [{"type": "count", "as": "n"}]},
                _dataset(licenseInfo="Public domain"),
            )
        )
        assert "  Data last edited: 2025-09-30" in text
        assert "  License: Public domain" in text

    def test_item_url_needs_a_safe_id(self, plugin):
        source = plugin._source({"id": "../x", "title": "T"}, f"{FS}/0", None)
        assert source["item_url"] == ""


class TestSchemaDetails:
    async def _schema(self, plugin, fields):
        _serve(plugin, SERVICE_INFO, {"fields": fields})
        return _body(await _tool(plugin, "get_schema", {}, _dataset()))

    async def test_string_length_and_coded_values(self, plugin):
        text = await self._schema(
            plugin,
            [
                {
                    "name": "STATUS",
                    "type": "esriFieldTypeString",
                    "alias": "Status",
                    "length": 2,
                    "domain": {
                        "type": "codedValue",
                        "codedValues": [
                            {"code": "A", "name": "Active"},
                            {"code": "I", "name": "Inactive"},
                        ],
                    },
                }
            ],
        )
        assert "  • STATUS (esriFieldTypeString, length 2)" in text
        assert "    Values: A = Active; I = Inactive" in text

    async def test_range_domain(self, plugin):
        text = await self._schema(
            plugin,
            [
                {
                    "name": "UNITS",
                    "type": "esriFieldTypeInteger",
                    "domain": {"type": "range", "range": [0, 500]},
                }
            ],
        )
        assert "  • UNITS (esriFieldTypeInteger)" in text
        assert "    Range: 0 to 500" in text

    async def test_long_domains_are_capped(self, plugin):
        values = [{"code": i, "name": f"V{i}"} for i in range(45)]
        text = await self._schema(
            plugin,
            [
                {
                    "name": "CODE",
                    "type": "esriFieldTypeInteger",
                    "domain": {"type": "codedValue", "codedValues": values},
                }
            ],
        )
        assert "29 = V29; … and 15 more" in text
        assert "30 = V30" not in text

    async def test_fields_without_extras_are_unchanged(self, plugin):
        schema = await self._schema(
            plugin, [{"name": "A", "type": "esriFieldTypeDouble", "length": 8}]
        )
        assert "  • A (esriFieldTypeDouble)" in schema
