"""Security tests for ArcGIS aggregate_data inputs (outStatistics, having)."""

from unittest.mock import AsyncMock, patch

import pytest

from plugins.arcgis.plugin import ArcGISPlugin

pytestmark = pytest.mark.security

INJECTIONS = [
    "x; DROP TABLE t",
    "x) OR (1=1",
    "x -- c",
    "UPPER(x)",
    "'x'",
    "a" * 100,
    "1x",
]


@pytest.fixture
def plugin():
    p = ArcGISPlugin(
        {"portal_url": "https://hub.arcgis.com", "city_name": "T", "timeout": 30}
    )
    p.feature_client = AsyncMock()
    return p


async def _run(plugin, arguments):
    with patch.object(plugin, "get_dataset", new_callable=AsyncMock) as get_ds:
        result = await plugin.execute_tool(
            "aggregate_data", {"dataset_id": "abc", **arguments}
        )
    return result, get_ds


class TestAggregateInputsBlockedBeforeRequest:
    @pytest.mark.parametrize("bad", INJECTIONS)
    async def test_statistic_field(self, plugin, bad):
        result, get_ds = await _run(
            plugin, {"statistics": [{"type": "sum", "field": bad}]}
        )
        assert result.success is False
        get_ds.assert_not_called()
        plugin.feature_client.get.assert_not_called()

    @pytest.mark.parametrize("bad", INJECTIONS)
    async def test_statistic_alias(self, plugin, bad):
        result, get_ds = await _run(
            plugin, {"statistics": [{"type": "count", "field": "A", "as": bad}]}
        )
        assert result.success is False
        get_ds.assert_not_called()

    @pytest.mark.parametrize("bad", INJECTIONS)
    async def test_group_by_field(self, plugin, bad):
        result, get_ds = await _run(
            plugin, {"statistics": [{"type": "count"}], "group_by": [bad]}
        )
        assert result.success is False
        get_ds.assert_not_called()

    @pytest.mark.parametrize(
        "bad",
        ["COUNT(*) > 1; DROP TABLE t", "COUNT(*) > 0 OR DELETE FROM t"],
    )
    async def test_having_clause(self, plugin, bad):
        result, get_ds = await _run(
            plugin, {"statistics": [{"type": "count"}], "having": bad}
        )
        assert result.success is False
        get_ds.assert_not_called()

    async def test_order_by(self, plugin):
        result, get_ds = await _run(
            plugin, {"statistics": [{"type": "count"}], "order_by": "x; DROP"}
        )
        assert result.success is False
        get_ds.assert_not_called()
