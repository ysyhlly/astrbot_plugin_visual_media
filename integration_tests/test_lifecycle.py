import inspect
import json
import subprocess
import sys
import typing
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_visual_media.main import VisualMediaPlugin


class Host:
    def __init__(self):
        self.registered_web_apis = []
        self.provider_manager = SimpleNamespace(llm_tools=SimpleNamespace(func_list=[]))
        self.platform_manager = SimpleNamespace(platform_insts=[], get_insts=lambda: [])

    def get_registered_star(self, name):
        return None

    def register_web_api(self, *route):
        self.registered_web_apis.append(route)

    def add_llm_tools(self, *tools):
        self.provider_manager.llm_tools.func_list.extend(tools)

    def get_config(self, *args, **kwargs):
        return {
            "provider_settings": {},
            "platform_settings": {},
            "platform": [],
            "admins_id": [],
        }

    def get_all_stars(self):
        return []

    def get_all_platforms(self):
        return []

    def get_all_providers(self):
        return []

    def get_all_embedding_providers(self):
        return []

    def get_provider_by_id(self, identifier):
        return None


@pytest.mark.asyncio
async def test_both_engines_start_and_stop_without_legacy_plugins(paths):
    host = Host()
    owner = VisualMediaPlugin(host, {})
    await owner.initialize()
    try:
        assert owner.memes.base_dir == paths / owner.name / "memes"
        assert Path(owner.generation._plugin_data_dir) == paths / owner.name
        assert len(host.registered_web_apis) >= 60
        assert all(
            route[0].startswith("/astrbot_plugin_visual_media/")
            for route in host.registered_web_apis
        )
        assert len(
            {(route[0], tuple(route[2])) for route in host.registered_web_apis}
        ) == len(host.registered_web_apis)
        assert {tool.name for tool in host.provider_manager.llm_tools.func_list} == {
            "gemini_image_generation",
            "gemini_image_batch",
            "gemini_image_lookup",
            "gemini_image_share",
        }
        assert all(
            tool.handler_module_path == owner.__class__.__module__
            for tool in host.provider_manager.llm_tools.func_list
        )
        assert not owner.get_service().get_status()["ready"]
        assert owner.memes.db_service.count_total() == 0
        assert owner.library.profiles() == []
    finally:
        await owner.terminate()
    assert host.registered_web_apis == []
    assert host.provider_manager.llm_tools.func_list == []
    assert owner.get_service().get_status()["state"] == "closed"
    await owner.terminate()


def test_one_plugin_registration():
    from astrbot.core.star.star import star_map

    registrations = [
        k for k in star_map if k.startswith("astrbot_plugin_visual_media.")
    ]
    assert registrations == ["astrbot_plugin_visual_media.main"]


def test_legacy_plugin_conflict_fails_before_registering():
    host = Host()
    host.get_registered_star = lambda name: SimpleNamespace(activated=True)
    with pytest.raises(RuntimeError, match="Disable"):
        VisualMediaPlugin(host, {})
    assert not host.registered_web_apis


def test_handler_inventory_matches_original_engines():
    root = Path(__file__).resolve().parents[1]
    inventory = json.loads((root / "handler_manifest.json").read_text())
    assert len(inventory) == 46
    for item in inventory:
        assert hasattr(VisualMediaPlugin, item["handler"])
        handler = getattr(VisualMediaPlugin, item["handler"])
        if inspect.isfunction(handler):
            typing.get_type_hints(handler)
        else:
            assert hasattr(handler, "parent_group")


def test_import_conflict_is_rejected_before_tool_registration():
    source = """
from types import SimpleNamespace
from astrbot.core.star.star import star_registry
from astrbot.core.provider.register import llm_tools
star_registry.append(SimpleNamespace(name="astrbot_plugin_stealer", activated=True, star_cls=object()))
before = list(llm_tools.func_list)
try:
    import astrbot_plugin_visual_media.main
except RuntimeError as error:
    assert "Disable astrbot_plugin_stealer" in str(error)
else:
    raise AssertionError("competing plugin was accepted")
assert list(llm_tools.func_list) == before
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_real_config_and_provider_hot_save_preserve_both_sections(paths):
    from astrbot.api import AstrBotConfig

    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text())
    file = paths / "config.json"
    config = AstrBotConfig(config_path=str(file), schema=schema)
    config["memes"]["auto_send_meme"] = False
    config["generation"]["provider_settings"]["provider_overrides"] = [
        {
            "__template_key": "openai_images",
            "name": "fixture",
            "model": "fixture-v1",
            "api_keys": ["test-only"],
            "api_base": "https://example.invalid",
        }
    ]
    config.save_config()
    plugin = VisualMediaPlugin(Host(), config)
    await plugin.initialize()
    try:
        service = plugin.generation.studio_providers
        snapshot = await service.get_config()
        payload = {
            "revision": snapshot["revision"],
            "provider_polling": snapshot["provider_polling"],
            "entries": [
                {
                    "id": entry["id"],
                    "api_type": entry["api_type"],
                    "values": {"model": "fixture-v2"},
                    "secret_actions": {},
                }
                for entry in snapshot["entries"]
            ],
            "common": {"values": {}, "secret_actions": {}},
        }
        await service.save_config(payload)
        assert plugin.get_service().get_status()["ready"]
        plugin.memes.plugin_config._data.save_config({"meme_chance": 0.33})
        disk = json.loads(file.read_text(encoding="utf-8-sig"))
        assert disk["memes"]["auto_send_meme"] is False
        assert disk["memes"]["meme_chance"] == 0.33
        assert (
            disk["generation"]["provider_settings"]["provider_overrides"][0]["model"]
            == "fixture-v2"
        )
    finally:
        await plugin.terminate()
