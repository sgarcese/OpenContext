"""Security tests for the ArcGIS ``order_by`` guard (orderByFields)."""

from unittest.mock import AsyncMock, patch

import pytest

from plugins.arcgis.plugin import ArcGISPlugin
from plugins.arcgis.where_validator import WhereValidator

pytestmark = pytest.mark.security


class TestValidateOrderBy:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("OBJECTID", "OBJECTID"),
            ("units desc", "units DESC"),
            ("  COUNTY ,UNITS Asc ", "COUNTY, UNITS ASC"),
            (None, None),
            ("", None),
            ("   ", None),
        ],
    )
    def test_accepts_field_names_with_direction(self, raw, expected):
        assert WhereValidator.validate_order_by(raw) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "x; DROP TABLE t",
            "x) OR 1=1",
            "x -- comment",
            "x /* c */",
            "UPPER(name)",
            "'name'",
            "name DESC NULLS FIRST",
            "1",
            "a" * 100,
            "a,",
            ",".join(["f"] * 11),
        ],
    )
    def test_rejects_anything_but_a_sort_order(self, bad):
        with pytest.raises(ValueError):
            WhereValidator.validate_order_by(bad)

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            WhereValidator.validate_order_by(["OBJECTID"])


class TestOrderByBlockedBeforeRequest:
    async def test_bad_order_by_never_reaches_the_service(self):
        plugin = ArcGISPlugin(
            {"portal_url": "https://hub.arcgis.com", "city_name": "T", "timeout": 30}
        )
        plugin.feature_client = AsyncMock()
        with patch.object(plugin, "get_dataset", new_callable=AsyncMock) as get_ds:
            result = await plugin.execute_tool(
                "query_data",
                {"dataset_id": "abc123", "order_by": "x; DROP TABLE t"},
            )
        assert result.success is False
        assert "Invalid order_by term" in result.error_message
        get_ds.assert_not_called()
        plugin.feature_client.get.assert_not_called()
