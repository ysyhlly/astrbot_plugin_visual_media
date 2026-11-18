import threading
from types import SimpleNamespace as NS

import pytest
from astrbot_plugin_visual_media import world_reference_access as access


class Config(dict):
    calls = 0

    def save_config(self):
        self.calls += 1


def test_explicit_grant_is_scoped_persistent_idempotent_and_preserves_other_config(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(access.StarTools, "get_data_dir", lambda name: tmp_path)
    cfg = Config(
        generation={
            "provider_settings": {"secret": "do-not-expose"},
            "image_generation_settings": {"llm_tool_reference_allowed_dirs": []},
        },
        memes={"enabled": True},
    )
    plugin = NS(
        raw_config=cfg,
        _config_save_lock=threading.RLock(),
        generation=NS(
            cfg=NS(
                llm_tool_reference_path_mode="whitelist",
                llm_tool_reference_allowed_dirs=[],
            )
        ),
    )
    directory = tmp_path / "initial/visual-assets"
    assert not access.status(plugin, directory)["allowed"]
    result = access.grant(plugin, directory)
    assert result["allowed"] and result["saved"] and "secret" not in str(result)
    assert cfg["generation"]["provider_settings"] == {
        "secret": "do-not-expose"
    } and cfg["memes"] == {"enabled": True}
    assert cfg["generation"]["image_generation_settings"][
        "llm_tool_reference_allowed_dirs"
    ] == [str(directory.resolve())]
    assert not access.grant(plugin, directory)["saved"] and cfg.calls == 1
    with pytest.raises(ValueError):
        access.grant(plugin, tmp_path.parent)


def test_failed_save_does_not_expand_runtime_or_persisted_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(access.StarTools, "get_data_dir", lambda name: tmp_path)
    cfg = Config(
        generation={
            "image_generation_settings": {"llm_tool_reference_allowed_dirs": []}
        }
    )
    cfg.save_config = lambda: (_ for _ in ()).throw(OSError("failed"))
    plugin = NS(
        raw_config=cfg,
        _config_save_lock=threading.RLock(),
        generation=NS(
            cfg=NS(
                llm_tool_reference_path_mode="whitelist",
                llm_tool_reference_allowed_dirs=[],
            )
        ),
    )
    with pytest.raises(OSError):
        access.grant(plugin, tmp_path / "initial/visual-assets")
    assert (
        cfg["generation"]["image_generation_settings"][
            "llm_tool_reference_allowed_dirs"
        ]
        == []
    )
    assert plugin.generation.cfg.llm_tool_reference_allowed_dirs == []
