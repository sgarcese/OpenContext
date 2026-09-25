"""Tests for ArcGIS query_data paging, total counts and output formats (#29)."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from plugins.arcgis.plugin import ArcGISPlugin

SERVICE_URL = "https://services.arcgis.com/xyz/FeatureServer/0"
DATASET = {
    "id": "abc123",
    "title": "LIHTC",
    "type": "Feature Layer",
    "service_url": SERVICE_URL,
}


@pytest.fixture
def plugin():
    """Plugin with a mock feature client and a stubbed get_dataset."""
    p = ArcGISPlugin(
        {"portal_url": "https://hub.arcgis.com", "city_name": "HUD", "timeout": 30}
    )
    p.feature_client = AsyncMock()
    return p


def _response(data):
    mock = Mock()
    mock.json.return_value = data
    mock.raise_for_status = Mock()
    mock.headers = Mock()
    mock.headers.get = Mock(return_value="application/json")
    return mock


def _rows(n, start=0):
    return [{"OBJECTID": i, "PROJECT": f"P{i}"} for i in range(start, start + n)]


def _features(rows, exceeded=False):
    data = {"features": [{"attributes": r} for r in rows]}
    if exceeded:
        data["exceededTransferLimit"] = True
    return data


async def _run(plugin, responses, arguments):
    """Execute query_data with the feature client answering ``responses``."""
    plugin.feature_client.get = AsyncMock(side_effect=[_response(r) for r in responses])
    with patch.object(
        plugin, "get_dataset", new_callable=AsyncMock, return_value=dict(DATASET)
    ):
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": "abc123", **arguments}
        )
    return result


def _params(plugin, call_index=0):
    return plugin.feature_client.get.call_args_list[call_index].kwargs["params"]


class TestQueryParameters:
    async def test_defaults_send_no_offset_or_order(self, plugin):
        await _run(plugin, [_features(_rows(3))], {})
        params = _params(plugin)
        assert "resultOffset" not in params
        assert "orderByFields" not in params

    async def test_offset_and_order_by_are_sent(self, plugin):
        await _run(
            plugin,
            [_features(_rows(10, start=20)), {"count": 45}],
            {"limit": 10, "offset": 20, "order_by": "county, units desc"},
        )
        params = _params(plugin)
        assert params["resultOffset"] == 20
        assert params["orderByFields"] == "county, units DESC"

    @pytest.mark.parametrize("offset", [-1, True, "5", 2.5])
    async def test_invalid_offset_is_refused(self, plugin, offset):
        result = await _run(plugin, [], {"offset": offset})
        assert result.success is False
        assert "offset" in result.error_message

    async def test_unknown_format_is_refused(self, plugin):
        result = await _run(plugin, [], {"format": "xml"})
        assert result.success is False
        assert "format must be one of" in result.error_message
        plugin.feature_client.get.assert_not_called()


class TestTotalCount:
    async def test_short_first_page_skips_count_request(self, plugin):
        """The HUD 39-of-50 case: the page proves the total; one request only."""
        result = await _run(plugin, [_features(_rows(39))], {"limit": 50})
        text = result.content[0]["text"]
        assert plugin.feature_client.get.call_count == 1
        assert "Returned 39 of 39 matching record(s) (offset: 0, limit: 50):" in text
        assert "Next page" not in text

    async def test_full_page_requests_count_and_gives_next_offset(self, plugin):
        result = await _run(
            plugin,
            [_features(_rows(10), exceeded=True), {"count": 222}],
            {"limit": 10},
        )
        text = result.content[0]["text"]
        count_params = _params(plugin, 1)
        assert count_params["returnCountOnly"] == "true"
        assert count_params["where"] == "1=1"
        assert "Returned 10 of 222 matching record(s)" in text
        assert "Next page: offset=10 " in text

    async def test_last_page_has_no_next_hint(self, plugin):
        result = await _run(
            plugin,
            [_features(_rows(2, start=220)), {"count": 222}],
            {"limit": 10, "offset": 220},
        )
        text = result.content[0]["text"]
        assert "Returned 2 of 222 matching record(s) (offset: 220, limit: 10):" in text
        assert "Next page" not in text

    async def test_count_failure_still_returns_rows(self, plugin):
        """A broken count endpoint falls back to exceededTransferLimit."""
        result = await _run(
            plugin,
            [_features(_rows(10), exceeded=True), {"error": {"code": 400}}],
            {"limit": 10},
        )
        text = result.content[0]["text"]
        assert result.success is True
        assert "Returned 10 record(s) (offset: 0, limit: 10):" in text
        assert "Next page: offset=10 " in text

    async def test_empty_page_past_the_end_reports_total(self, plugin):
        result = await _run(
            plugin, [_features([]), {"count": 5}], {"limit": 10, "offset": 50}
        )
        assert (
            "No records returned at offset 50; 5 record(s) match"
            in (result.content[0]["text"])
        )


class TestOutputFormats:
    @staticmethod
    def _json_block(text):
        start = text.index("\n[\n") + 1
        end = text.index("\n]", start) + 2
        return json.loads(text[start:end])

    async def test_json_rows_parse_and_keep_numbers(self, plugin):
        rows = [{"PROJECT": "Oak Park", "LI_UNITS": 40, "ACTIVE": True, "X": None}]
        result = await _run(plugin, [_features(rows)], {"format": "json"})
        parsed = self._json_block(result.content[0]["text"])
        assert parsed == rows

    async def test_json_escapes_newlines_in_values(self, plugin):
        rows = [{"NOTE": "line one\nRecord 2:\n  admin: true"}]
        result = await _run(plugin, [_features(rows)], {"format": "json"})
        text = result.content[0]["text"]
        assert "\nRecord 2:" not in text
        assert self._json_block(text)[0]["NOTE"].startswith("line one")

    async def test_csv_has_header_and_one_line_per_record(self, plugin):
        rows = [
            {"PROJECT": "Oak, Park", "LI_UNITS": 40},
            {"PROJECT": "Two\nLines", "LI_UNITS": None},
        ]
        result = await _run(plugin, [_features(rows)], {"format": "csv"})
        text = result.content[0]["text"]
        lines = text[text.index("PROJECT,LI_UNITS") :].split("\n")
        assert lines[0] == "PROJECT,LI_UNITS"
        assert lines[1] == '"Oak, Park",40'
        assert lines[2] == "Two Lines,"


class TestRenderRowsBudget:
    @pytest.mark.parametrize("fmt", ["json", "csv"])
    def test_budget_drops_whole_rows(self, plugin, fmt):
        rows = [{"v": "x" * 100} for _ in range(10)]
        text, shown = plugin.render_rows(rows, fmt, max_chars=400)
        assert 1 <= shown < 10
        assert f"Showing {shown} of 10 record(s)" in text
        if fmt == "json":
            body = text[: text.index("\n]") + 2]
            assert len(json.loads(body)) == shown

    def test_budget_notice_changes_next_offset(self, plugin):
        page = {"records": [{"v": "x" * 3_000} for _ in range(40)], "offset": 100}
        page["total"] = 500
        text = plugin._format_query_results(page, 40)
        shown = text.count("Record ")
        assert f"Next page: offset={100 + shown} " in text
