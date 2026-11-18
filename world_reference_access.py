"""Explicit administrator grant for World-owned reference images only."""
import copy
import json
import re
from pathlib import Path

from astrbot.core.star.star_tools import StarTools


def expected_directory(plugin=None):
    context = getattr(plugin, "context", None)
    get_star = getattr(context, "get_registered_star", None)
    if callable(get_star):
        world = get_star("astrbot_plugin_dynamics_world")
        star = getattr(world, "star_cls", world)
        get_dir = getattr(star, "get_reference_assets_directory", None)
        if callable(get_dir):
            resolved = get_dir()
            if resolved:
                return Path(str(resolved)).resolve()
    root = Path(StarTools.get_data_dir("astrbot_plugin_dynamics_world"))
    pointer = root / "active.json"
    generation = json.loads(pointer.read_text("utf-8"))["generation"] if pointer.exists() else "initial"
    if generation != "initial" and not re.fullmatch(r"import-[a-f0-9]{32}", generation):
        raise ValueError("Invalid World generation")
    return (root / generation / "visual-assets").resolve()


def status(plugin, directory):
    supplied = Path(directory).resolve()
    if supplied != expected_directory(plugin):
        raise ValueError("Only the active World visual-assets directory can be granted")
    cfg = plugin.generation.cfg
    allowed = cfg.llm_tool_reference_path_mode == "global" or any(
        supplied.is_relative_to(Path(p).resolve()) for p in cfg.llm_tool_reference_allowed_dirs
    )
    return {"api_version": 1, "allowed": allowed, "directory": str(supplied)}


def grant(plugin, directory):
    add_dir = getattr(plugin, "add_reference_allowed_dir", None)
    if callable(add_dir):
        report = status(plugin, directory)
        if report["allowed"]:
            return dict(report, saved=False)
        saved = bool(add_dir(report["directory"]))
        return dict(report, allowed=True, saved=saved)
    with plugin._config_save_lock:
        report = status(plugin, directory)
        if report["allowed"]:
            return dict(report, saved=False)
        save = getattr(plugin.raw_config, "save_config", None)
        if not callable(save):
            raise ValueError("Host configuration cannot be saved")
        generation = plugin.raw_config.setdefault("generation", {})
        previous = copy.deepcopy(generation.get("image_generation_settings", {}))
        updated = copy.deepcopy(previous)
        updated["llm_tool_reference_allowed_dirs"] = list(
            dict.fromkeys([*updated.get("llm_tool_reference_allowed_dirs", []), report["directory"]])
        )
        try:
            generation["image_generation_settings"] = updated
            save()
        except Exception:
            generation["image_generation_settings"] = previous
            raise
        plugin.generation.cfg.llm_tool_reference_allowed_dirs = list(updated["llm_tool_reference_allowed_dirs"])
        return dict(report, allowed=True, saved=True)
