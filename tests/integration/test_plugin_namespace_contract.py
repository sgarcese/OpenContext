"""Hermetic integration: PluginManager discovery and namespaced tools."""

from __future__ import annotations

import pytest

from core.plugin_manager import PluginManager


@pytest.mark.asyncio
async def test_load_fake_plugin_registers_prefixed_tools(
    integration_fake_config_dict: dict,
) -> None:
    pm = PluginManager(integration_fake_config_dict)
    await pm.load_plugins()

    names = {t["name"] for t in pm.get_all_tools()}
    assert "integration_test_fake__echo" in names
    plugin_name, tool_def = pm.tools["integration_test_fake__echo"]
    assert plugin_name == "integration_test_fake"
    assert tool_def.name == "echo"

    result = await pm.execute_tool(
        "integration_test_fake__echo", {"msg": "namespace-ok"}
    )
    assert result.success is True
    await pm.shutdown()


@pytest.mark.asyncio
async def test_configuration_error_when_multiple_plugins_enabled(
    integration_fake_config_dict: dict,
) -> None:
    from core.validators import ConfigurationError

    bad = dict(integration_fake_config_dict)
    bad["plugins"] = dict(bad["plugins"])
    bad["plugins"]["ckan"] = {"enabled": True, "base_url": "https://x.example"}

    pm = PluginManager(bad)
    with pytest.raises(ConfigurationError):
        await pm.load_plugins()
