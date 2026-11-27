"""Dynamics Event Bus subscriber for ``vibe_transition`` serious-mode meme suppression."""

from __future__ import annotations

import threading
import time
from typing import Any

from astrbot.api import logger

from ..util.series_config import load_series_panels

SERIOUS_VIBES = frozenset(
    {
        "serious_inquiry",
        "deciding",
        "serious",
        "support",
        "urgent",
        "grief",
        "conflict",
        "formal",
        "deep_discussion",
    }
)
RELAXED_VIBES = frozenset(
    {
        "playful",
        "casual",
        "lively",
        "warm",
        "idle",
        "banter",
        "relaxed",
        "normal",
        "fun",
        "chill",
        "quiet",
        "chat",
    }
)

DEFAULT_SERIOUS_TTL_SECONDS = 600.0


class VibeStateCache:
    """Thread-safe per-group/UMO serious-vibe state cache with automatic TTL expiry."""

    def __init__(self, *, default_ttl: float = DEFAULT_SERIOUS_TTL_SECONDS) -> None:
        self._default_ttl = max(30.0, float(default_ttl))
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _extract_keys(payload: dict[str, Any]) -> list[str]:
        keys: list[str] = []
        for field in ("umo", "unified_msg_origin", "scope", "session_id"):
            val = str(payload.get(field) or "").strip()
            if val and val not in keys:
                keys.append(val)
        gid = str(payload.get("group_id") or "").strip()
        if gid:
            gkey = f"group:{gid}"
            if gkey not in keys:
                keys.append(gkey)
        # Derive group key from UMO as fallback match
        for k in list(keys):
            parts = k.split(":", 2)
            if len(parts) == 3 and "group" in parts[1].lower() and parts[2].strip():
                gkey = f"group:{parts[2].strip()}"
                if gkey not in keys:
                    keys.append(gkey)
        return keys

    @staticmethod
    def _resolve_vibe(payload: dict[str, Any]) -> tuple[bool, str]:
        raw_vibe = str(
            payload.get("vibe")
            or payload.get("to_vibe")
            or payload.get("new_vibe")
            or payload.get("to")
            or payload.get("state")
            or payload.get("mode")
            or ""
        ).strip().lower()

        explicit = payload.get("serious_mode")
        if isinstance(explicit, bool):
            return explicit, (raw_vibe or ("serious" if explicit else "relaxed"))

        if raw_vibe in SERIOUS_VIBES:
            return True, raw_vibe
        if raw_vibe in RELAXED_VIBES:
            return False, raw_vibe
        if any(tok in raw_vibe for tok in ("serious", "decid", "urgent", "grief", "conflict")):
            return True, raw_vibe
        return False, raw_vibe or "relaxed"

    def update_from_payload(self, payload: Any, *, now: float | None = None) -> dict[str, Any]:
        """Consume a ``vibe_transition`` event payload and update cached serious state."""
        if not isinstance(payload, dict):
            return {"updated": False, "serious": False, "vibe": ""}
        keys = self._extract_keys(payload)
        if not keys:
            return {"updated": False, "serious": False, "vibe": ""}

        serious, vibe = self._resolve_vibe(payload)
        ts = time.monotonic() if now is None else float(now)
        raw_ttl = payload.get("ttl_seconds", payload.get("ttl"))
        try:
            ttl = max(10.0, min(3600.0, float(raw_ttl))) if raw_ttl is not None else self._default_ttl
        except (TypeError, ValueError):
            ttl = self._default_ttl

        with self._lock:
            self._prune_locked(ts)
            if serious:
                record = {
                    "serious": True,
                    "vibe": vibe,
                    "updated_at": ts,
                    "expires_at": ts + ttl,
                }
                for key in keys:
                    self._states[key] = dict(record)
            else:
                for key in keys:
                    self._states.pop(key, None)
        return {"updated": True, "keys": keys, "serious": serious, "vibe": vibe}

    def _prune_locked(self, now: float) -> None:
        expired = [
            k
            for k, v in self._states.items()
            if float(v.get("expires_at", 0.0)) <= now
        ]
        for k in expired:
            self._states.pop(k, None)

    def is_serious(
        self,
        umo: str = "",
        *,
        group_id: str = "",
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Return ``(is_serious, vibe_name)`` for the given scope, respecting TTL expiry."""
        ts = time.monotonic() if now is None else float(now)
        candidates: list[str] = []
        clean_umo = str(umo or "").strip()
        if clean_umo:
            candidates.append(clean_umo)
            parts = clean_umo.split(":", 2)
            if len(parts) == 3 and "group" in parts[1].lower() and parts[2].strip():
                candidates.append(f"group:{parts[2].strip()}")
        clean_gid = str(group_id or "").strip()
        if clean_gid:
            gkey = f"group:{clean_gid}"
            if gkey not in candidates:
                candidates.append(gkey)
        if not candidates:
            return False, ""

        with self._lock:
            for key in candidates:
                entry = self._states.get(key)
                if not entry:
                    continue
                if float(entry.get("expires_at", 0.0)) <= ts:
                    self._states.pop(key, None)
                    continue
                if entry.get("serious"):
                    return True, str(entry.get("vibe") or "serious")
        return False, ""

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        ts = time.monotonic()
        with self._lock:
            self._prune_locked(ts)
            return {k: dict(v) for k, v in self._states.items()}


class VibeEventBusBridge:
    """Zero-coupling bridge subscribing ``visual_media`` to ``chat_dynamics`` Event Bus."""

    EVENT_TYPE = "vibe_transition"
    OWNER_ID = "astrbot_plugin_visual_media"

    def __init__(self, owner: Any, *, default_ttl: float = DEFAULT_SERIOUS_TTL_SECONDS) -> None:
        self.owner = owner
        self.cache = VibeStateCache(default_ttl=default_ttl)
        self._token: str = ""
        self._bus_owner: Any = None
        self._lock = threading.RLock()

    def _find_chat_dynamics_owner(self) -> Any:
        ctx = getattr(self.owner, "context", None)
        if ctx is None:
            return None
        sp = load_series_panels()
        if sp is not None:
            finder = getattr(sp, "_find_chat_dynamics_owner", None)
            if callable(finder):
                try:
                    found = finder(ctx)
                    if found is not None:
                        return found
                except Exception:
                    pass
        get_star = getattr(ctx, "get_registered_star", None)
        if callable(get_star):
            try:
                meta = get_star("astrbot_plugin_chat_dynamics")
                inst = getattr(meta, "star_cls", None) if meta is not None else None
                if inst is not None:
                    return inst
            except Exception:
                pass
        get_all = getattr(ctx, "get_all_stars", None)
        if callable(get_all):
            try:
                stars = get_all() or {}
                iterable = stars.values() if isinstance(stars, dict) else stars
                for meta in iterable:
                    if str(getattr(meta, "name", "") or "") == "astrbot_plugin_chat_dynamics":
                        inst = getattr(meta, "star_cls", None)
                        if inst is not None:
                            return inst
            except Exception:
                pass
        return None

    def handle_vibe_transition(self, payload: Any) -> dict[str, Any]:
        """Callback invoked by ``chat_dynamics`` Event Bus when group vibe changes."""
        try:
            result = self.cache.update_from_payload(payload)
            if result.get("updated"):
                logger.debug(
                    "[VisualMedia] vibe_transition updated serious=%s vibe=%s keys=%s",
                    result.get("serious"),
                    result.get("vibe"),
                    result.get("keys"),
                )
            return result
        except Exception as exc:
            logger.debug("[VisualMedia] vibe_transition callback ignored error: %s", exc)
            return {"updated": False, "serious": False, "vibe": ""}

    def subscribe(self) -> bool:
        """Idempotently subscribe to ``chat_dynamics.subscribe_dynamics_event``."""
        with self._lock:
            if self._token and self._bus_owner is not None:
                return True
            cd_owner = self._find_chat_dynamics_owner()
            if cd_owner is None:
                return False
            sub_fn = getattr(cd_owner, "subscribe_dynamics_event", None)
            if not callable(sub_fn):
                return False
            try:
                token = sub_fn(
                    self.EVENT_TYPE,
                    self.handle_vibe_transition,
                    owner=self.OWNER_ID,
                )
            except TypeError:
                try:
                    token = sub_fn(self.EVENT_TYPE, self.handle_vibe_transition)
                except Exception as exc:
                    logger.debug("[VisualMedia] subscribe_dynamics_event failed: %s", exc)
                    return False
            except Exception as exc:
                logger.debug("[VisualMedia] subscribe_dynamics_event failed: %s", exc)
                return False
            self._token = str(token or "").strip() or "subscribed"
            self._bus_owner = cd_owner
            logger.info(
                "[VisualMedia] Subscribed to Dynamics Event Bus: event=%s token=%s",
                self.EVENT_TYPE,
                self._token,
            )
            return True

    def unsubscribe(self) -> bool:
        """Safely unsubscribe on plugin terminate/reload without raising."""
        with self._lock:
            token = self._token
            bus_owner = self._bus_owner
            self._token = ""
            self._bus_owner = None
            self.cache.clear()
        if not token or bus_owner is None:
            return False
        unsub_fn = getattr(bus_owner, "unsubscribe_dynamics_event", None)
        if not callable(unsub_fn):
            return False
        try:
            return bool(unsub_fn(token))
        except Exception as exc:
            logger.debug("[VisualMedia] unsubscribe_dynamics_event failed: %s", exc)
            return False


def is_vibe_serious_for_event(plugin: Any, event: Any) -> tuple[bool, str]:
    """Check whether the event's group/UMO is currently in serious vibe suppression mode."""
    if plugin is None or event is None:
        return False, ""
    cache = getattr(plugin, "vibe_state", None)
    if cache is None:
        bus = getattr(plugin, "vibe_bus", None)
        if bus is None:
            bus = getattr(getattr(plugin, "owner", None), "vibe_bus", None)
        cache = getattr(bus, "cache", None) if bus is not None else None
    if cache is None or not callable(getattr(cache, "is_serious", None)):
        return False, ""

    umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
    gid = ""
    get_gid = getattr(event, "get_group_id", None)
    if callable(get_gid):
        try:
            gid = str(get_gid() or "").strip()
        except Exception:
            gid = ""
    if not gid:
        gid = str(getattr(event, "group_id", "") or "").strip()
    return cache.is_serious(umo=umo, group_id=gid)
