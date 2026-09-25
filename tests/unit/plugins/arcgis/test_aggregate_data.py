"""Tests for the ArcGIS aggregate_data tool (#31).

Modelled on HUD's Housing Choice Vouchers by Tract layer: sum HCV_PUBLIC and
count tracts, grouped by state and county.
"""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from plugins.arcgis.plugin import ArcGISPlugin

LAYER = "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/HCV/FeatureServer/0"
FIELDS = [
    {"name": "OBJECTID", "type": "esriFieldTypeOID"},
    {"name": "GEOID", "type": "esriFieldTypeString"},
    {"name": "STATE", "type": "esriFieldTypeString"},
    {"name": "COUNTY", "type": "esriFieldTypeString"},
    {"name": "HCV_PUBLIC", "type": "esriFieldTypeInteger"},
]
METADATA = {
    "fields": FIELDS,
    "advancedQueryCapabilities": {"supportsStatistics": True},
}
GROUPS = {
    "features": [
        {
            "attributes": {
                "STATE": "18",
                "COUNTY": "141",
                "vouchers": 2609,
                "tracts": 82,
            }
        },
        {"attributes": {"STATE": "18", "COUNTY": "039", "vouchers": 767, "tracts": 45}},
    ]
}
FIVE = "STATE||COUNTY IN ('18141','18039','18091','18099','26021')"


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


async def _aggregate(plugin, arguments, *payloads):
    plugin.feature_client.get = AsyncMock(side_effect=[_response(p) for p in payloads])
    dataset = {"id": "abc", "type": "Feature Layer", "service_url": LAYER}
    with patch.object(
        plugin, "get_dataset", new_callable=AsyncMock, return_value=dataset
    ):
        return await plugin.execute_tool(
            "aggregate_data", {"dataset_id": "abc", **arguments}
        )


def _query_params(plugin):
    call = plugin.feature_client.get.call_args_list[-1]
    assert call.args[0] == f"{LAYER}/query"
    return call.kwargs["params"]


VOUCHERS = {
    "statistics": [
        {"type": "sum", "field": "HCV_PUBLIC", "as": "vouchers"},
        {"type": "count", "field": "GEOID", "as": "tracts"},
    ],
    "group_by": ["STATE", "COUNTY"],
    "where": FIVE,
}


class TestRequest:
    async def test_builds_out_statistics_and_group_by(self, plugin):
        result = await _aggregate(plugin, VOUCHERS, METADATA, GROUPS)
        assert result.success is True
        params = _query_params(plugin)
        assert json.loads(params["outStatistics"]) == [
            {
                "statisticType": "sum",
                "onStatisticField": "HCV_PUBLIC",
                "outStatisticFieldName": "vouchers",
            },
            {
                "statisticType": "count",
                "onStatisticField": "GEOID",
                "outStatisticFieldName": "tracts",
            },
        ]
        assert params["groupByFieldsForStatistics"] == "STATE,COUNTY"
        assert params["where"] == FIVE
        assert "havingClause" not in params
        assert "orderByFields" not in params

    async def test_fields_match_schema_case_insensitively(self, plugin):
        args = {
            "statistics": [{"type": "SUM", "field": "hcv_public"}],
            "group_by": ["county"],
        }
        await _aggregate(plugin, args, METADATA, GROUPS)
        params = _query_params(plugin)
        stat = json.loads(params["outStatistics"])[0]
        assert stat["onStatisticField"] == "HCV_PUBLIC"
        assert stat["outStatisticFieldName"] == "sum_HCV_PUBLIC"
        assert params["groupByFieldsForStatistics"] == "COUNTY"

    async def test_count_without_field_uses_object_id(self, plugin):
        await _aggregate(plugin, {"statistics": [{"type": "count"}]}, METADATA, GROUPS)
        stat = json.loads(_query_params(plugin)["outStatistics"])[0]
        assert stat["onStatisticField"] == "OBJECTID"

    async def test_having_and_order_by_are_sent(self, plugin):
        args = {
            **VOUCHERS,
            "having": "SUM(HCV_PUBLIC) > 100",
            "order_by": "vouchers desc",
        }
        await _aggregate(plugin, args, METADATA, GROUPS)
        params = _query_params(plugin)
        assert params["havingClause"] == "SUM(HCV_PUBLIC) > 100"
        assert params["orderByFields"] == "vouchers DESC"

    async def test_layer_argument_selects_a_table(self, plugin):
        service = LAYER.rsplit("/", 1)[0]
        info = {"layers": [{"id": 0, "name": "Tracts"}], "tables": [{"id": 1}]}
        plugin.feature_client.get = AsyncMock(
            side_effect=[_response(info), _response(METADATA), _response(GROUPS)]
        )
        dataset = {"id": "abc", "type": "Feature Service", "service_url": service}
        with patch.object(
            plugin, "get_dataset", new_callable=AsyncMock, return_value=dataset
        ):
            result = await plugin.execute_tool(
                "aggregate_data",
                {"dataset_id": "abc", "layer": 1, "statistics": [{"type": "count"}]},
            )
        assert result.success is True
        urls = [c.args[0] for c in plugin.feature_client.get.call_args_list]
        assert urls == [service, f"{service}/1", f"{service}/1/query"]


class TestOutput:
    async def test_text_output_lists_each_group(self, plugin):
        result = await _aggregate(plugin, VOUCHERS, METADATA, GROUPS)
        text = result.content[0]["text"]
        assert "Aggregated 2 group(s):" in text
        assert "vouchers: 2609" in text
        assert "tracts: 45" in text
        assert "Statistics ignore null values" in text

    async def test_csv_output(self, plugin):
        result = await _aggregate(
            plugin, {**VOUCHERS, "format": "csv"}, METADATA, GROUPS
        )
        text = result.content[0]["text"]
        assert "STATE,COUNTY,vouchers,tracts\n18,141,2609,82\n18,039,767,45" in text

    async def test_limit_trims_groups_with_notice(self, plugin):
        result = await _aggregate(plugin, {**VOUCHERS, "limit": 1}, METADATA, GROUPS)
        text = result.content[0]["text"]
        assert "Aggregated 2 group(s); showing the first 1 (limit):" in text
        assert "COUNTY: 039" not in text

    async def test_transfer_limit_is_reported(self, plugin):
        payload = {**GROUPS, "exceededTransferLimit": True}
        result = await _aggregate(plugin, VOUCHERS, METADATA, payload)
        assert "hit its transfer limit" in result.content[0]["text"]

    async def test_no_groups(self, plugin):
        result = await _aggregate(plugin, VOUCHERS, METADATA, {"features": []})
        assert result.success is True
        assert "No groups matched." in result.content[0]["text"]


class TestRefusals:
    async def test_unknown_field_names_the_problem(self, plugin):
        args = {"statistics": [{"type": "sum", "field": "NOPE"}]}
        result = await _aggregate(plugin, args, METADATA)
        assert result.success is False
        assert "Unknown statistics field 'NOPE'" in result.error_message

    async def test_unknown_group_by_field(self, plugin):
        args = {"statistics": [{"type": "count"}], "group_by": ["TRACT"]}
        result = await _aggregate(plugin, args, METADATA)
        assert "Unknown group_by field 'TRACT'" in result.error_message

    async def test_layer_without_statistics_support(self, plugin):
        metadata = {
            **METADATA,
            "advancedQueryCapabilities": {"supportsStatistics": False},
        }
        result = await _aggregate(plugin, {"statistics": [{"type": "count"}]}, metadata)
        assert result.success is False
        assert "does not support server-side statistics" in result.error_message

    async def test_duplicate_output_names(self, plugin):
        args = {
            "statistics": [
                {"type": "sum", "field": "HCV_PUBLIC", "as": "n"},
                {"type": "count", "field": "GEOID", "as": "N"},
            ]
        }
        result = await _aggregate(plugin, args, METADATA)
        assert "used twice" in result.error_message

    async def test_alias_clashing_with_group_field(self, plugin):
        args = {
            "statistics": [{"type": "count", "as": "county"}],
            "group_by": ["COUNTY"],
        }
        result = await _aggregate(plugin, args, METADATA)
        assert "used twice" in result.error_message

    async def test_sum_needs_a_field(self, plugin):
        result = await _aggregate(plugin, {"statistics": [{"type": "sum"}]}, METADATA)
        assert "sum needs a field" in result.error_message

    @pytest.mark.parametrize(
        "statistics",
        [[], "sum", [{"type": "median", "field": "X"}], ["sum"], [{"field": "X"}]],
    )
    async def test_malformed_statistics_never_reach_the_service(
        self, plugin, statistics
    ):
        result = await _aggregate(plugin, {"statistics": statistics})
        assert result.success is False
        plugin.feature_client.get.assert_not_called()

    @pytest.mark.parametrize("fmt", ["xml", "TEXT"])
    async def test_unknown_format(self, plugin, fmt):
        result = await _aggregate(plugin, {**VOUCHERS, "format": fmt})
        assert "format must be one of" in result.error_message
