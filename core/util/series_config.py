"""Helpers for reading extended series shared configuration without modifying shared files."""

from __future__ import annotations

import inspect
from typing import Any


def load_series_panels():
    """Load the sibling series_panels module in both package and top-level test imports."""
    try:
        if __package__ and __package__.count(".") >= 2:
            from ... import series_panels

            return series_panels
    except Exception:
        pass
    try:
        import series_panels

        return series_panels
    except Exception:
        return None


def _normalize_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("\r", "\n").replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, (str, int)):
            clean = str(item).strip()
            if clean and clean not in seen:
                seen.add(clean)
                out.append(clean)
    return out


def get_extended_series_shared_config(context: Any) -> dict[str, Any]:
    """Read series shared defaults via ``series_panels.get_series_shared_config(context)``
    while preserving extended provider slots (``vision_provider``, ``light_analysis_provider``,
    ``embedding_provider``, ``rerank_provider``, ``bot_names``) even before shared assets sync.
    """
    if context is None:
        return {}
    merged: dict[str, Any] = {}
    sp = load_series_panels()
    if sp is not None:
        getter = getattr(sp, "get_series_shared_config", None)
        if callable(getter):
            try:
                data = getter(context)
                if isinstance(data, dict):
                    merged.update(data)
            except Exception:
                pass
        finder = getattr(sp, "_find_chat_dynamics_owner", None)
        owner = None
        if callable(finder):
            try:
                owner = finder(context)
            except Exception:
                owner = None
        if owner is not None:
            raw_getter = getattr(owner, "get_series_shared_config", None)
            if callable(raw_getter) and not inspect.iscoroutinefunction(raw_getter):
                try:
                    raw = raw_getter()
                except Exception:
                    raw = None
                if isinstance(raw, dict) and raw.get("enabled") is not False:
                    for key in (
                        "light_analysis_provider",
                        "vision_provider",
                        "embedding_provider",
                        "rerank_provider",
                        "reply_provider",
                        "decision_provider",
                        "persona_id",
                    ):
                        if not merged.get(key):
                            val = raw.get(key)
                            if isinstance(val, str) and val.strip():
                                merged[key] = val.strip()
                    if not merged.get("bot_names"):
                        names = _normalize_str_list(raw.get("bot_names"))
                        if names:
                            merged["bot_names"] = names
    return merged


def resolve_shared_provider(
    context: Any,
    explicit: str = "",
    *,
    keys: tuple[str, ...],
) -> str:
    """Resolve an explicit provider ID or fall back in order across series shared config keys."""
    own = str(explicit or "").strip()
    if own:
        return own
    shared = get_extended_series_shared_config(context)
    for key in keys:
        val = str(shared.get(key) or "").strip()
        if val:
            return val
    return ""


def get_series_bot_names(context: Any) -> list[str]:
    """Return non-empty bot names published in series shared configuration."""
    shared = get_extended_series_shared_config(context)
    return _normalize_str_list(shared.get("bot_names"))
