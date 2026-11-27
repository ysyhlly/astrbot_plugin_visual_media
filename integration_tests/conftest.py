"""Integration tests use the real AstrBot runtime, isolated data and no providers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
import pytest_asyncio
from astrbot_plugin_visual_media import generation_engine
from astrbot_plugin_visual_media.main import VisualMediaPlugin

from astrbot.api.star import StarTools


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(
        StarTools, "get_data_dir", staticmethod(lambda name: tmp_path / name)
    )
    monkeypatch.setattr(
        generation_engine, "get_astrbot_plugin_data_path", lambda: str(tmp_path)
    )
    return tmp_path


@pytest_asyncio.fixture
async def owner(paths):
    from test_lifecycle import Host

    plugin = VisualMediaPlugin(Host(), {})
    await plugin.initialize()
    try:
        yield plugin
    finally:
        await plugin.terminate()
