"""Request-owned candidate search on a detached event; no send or injection."""

import asyncio
import copy
import hmac
import json
import secrets
import time
from collections import OrderedDict


class EmptyCatalogueCache:
    def __init__(self, *, ttl_seconds=30, max_entries=256):
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._secret = secrets.token_bytes(32)
        self._entries = OrderedDict()

    def key(self, plugin, event, query):
        try:
            scope, target = plugin.plugin_config.get_event_target(event)
            identity = (
                event.get_platform_id(),
                event.get_self_id(),
                event.get_sender_id(),
                event.unified_msg_origin,
                scope,
                target,
            )
        except Exception:
            return None
        if not all(identity):
            return None
        payload = json.dumps((*map(str, identity), query)).encode("utf-8")
        return hmac.digest(self._secret, payload, "sha256")

    def check(self, key, version):
        entry = self._entries.get(key)
        if entry is None:
            return "cold"
        stored_version, expires = entry
        if stored_version != version:
            self._entries.pop(key)
            return "version_changed"
        if time.monotonic() >= expires:
            self._entries.pop(key)
            return "expired"
        self._entries.move_to_end(key)
        return "hit"

    def remember(self, key, version):
        self._entries[key] = (version, time.monotonic() + self._ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


def joint_search_enabled(plugin, event):
    config = plugin.plugin_config
    return (
        getattr(plugin, "_terminated", False) is not True
        and getattr(config, "llm_joint_reply", True)
        and getattr(config, "auto_send_meme", True)
        and plugin.is_send_enabled_for_event(event)
    )


class JointReplyPrefetch:
    timeout_seconds = 6

    def __init__(self, plugin, event, query):
        self.plugin, self.event, self.query = plugin, event, query
        self.started = time.monotonic()
        self.outcome = "pending"
        self.cache_reason = "bypass"
        self.task = asyncio.create_task(
            asyncio.wait_for(self._search(), timeout=self.timeout_seconds)
        )
        jobs = getattr(plugin, "_joint_reply_prefetches", None)
        if jobs is None:
            jobs = set()
            plugin._joint_reply_prefetches = jobs
        jobs.add(self)

        def done(task):
            jobs.discard(self)
            if not task.cancelled():
                task.exception()

        self.task.add_done_callback(done)

    async def _library_version(self):
        try:
            return await asyncio.to_thread(self.plugin.db_service.get_library_version)
        except Exception:
            return None

    async def _search(self):
        if not self.query:
            return "empty_query", [], None
        if not joint_search_enabled(self.plugin, self.event):
            return "disabled", [], None

        cache = getattr(self.plugin, "_joint_empty_catalogue_cache", None)
        if cache is None:
            cache = self.plugin._joint_empty_catalogue_cache = EmptyCatalogueCache()
        key = cache.key(self.plugin, self.event, self.query)
        version = await self._library_version() if key is not None else None
        if key is None:
            self.cache_reason = "identity_unavailable"
        elif version is None:
            self.cache_reason = "version_unavailable"
        else:
            self.cache_reason = cache.check(key, version)
            if self.cache_reason == "hit":
                return "empty_catalogue", [], version

        clone = copy.copy(self.event)
        clone._extras = dict(getattr(self.event, "_extras", {}) or {})
        clone._extras.pop("provider_request", None)
        for name in (
            "_cd_deferred_reply_media_v1",
            "_stealer_passive_delivery_v1",
            "_stealer_joint_meme_plan_v1",
        ):
            if hasattr(clone, name):
                delattr(clone, name)
        clone._emoji_turn_state = {}
        clone._result = None
        clone._temporary_local_files = []

        async def no_send(*args, **kwargs):
            raise RuntimeError("prefetch_cannot_send")

        clone.send = no_send

        outcome, observed_version = "unconfirmed_empty", None

        def report(search_outcome, library_version):
            nonlocal outcome, observed_version
            outcome, observed_version = search_outcome, library_version

        async for _ in self.plugin._search_meme_impl(
            clone, self.query, on_outcome=report
        ):
            pass
        candidates = self.plugin._emoji_sender_engine.emoji_turn_state(
            clone
        ).get_candidates()
        return outcome, candidates[:5], observed_version

    async def _join(self):
        outcome, candidates, version = await asyncio.shield(self.task)
        # The early hook can finish before later hooks or a library import commits.
        if version is not None and await self._library_version() != version:
            return await asyncio.wait_for(self._search(), timeout=self.timeout_seconds)
        return outcome, candidates, version

    async def result(self, plugin, event, query, *, wait_seconds=None):
        if (
            self.plugin is not plugin
            or self.event is not event
            or self.query != query
            or getattr(plugin, "_terminated", False) is True
        ):
            return None
        try:
            outcome, result, version = (
                await self._join()
                if wait_seconds is None
                else await asyncio.wait_for(self._join(), timeout=wait_seconds)
            )
            if not joint_search_enabled(plugin, event):
                self.outcome = "disabled"
                return []
            allowed = [
                dict(row)
                for row in result
                if plugin.meme_selector.is_path_allowed_for_event(row["path"], event)
            ]
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.outcome = "timeout"
            return []
        except Exception:
            self.outcome = "error"
            return []
        self.outcome = "filtered" if result and not allowed else outcome
        if (
            self.outcome == "empty_catalogue"
            and version is not None
            and self.cache_reason != "hit"
        ):
            cache = plugin._joint_empty_catalogue_cache
            key = cache.key(plugin, event, query)
            if key is not None:
                cache.remember(key, version)
        return allowed

    async def close(self):
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def start_joint_prefetch(plugin, event):
    if (
        getattr(event, "_cd_joint_meme_supported_v1", False) is not True
        or plugin._emoji_sender_engine.is_command_event(event)
        or not joint_search_enabled(plugin, event)
    ):
        return None
    existing = getattr(event, "_stealer_joint_prefetch_v1", None)
    query = str(getattr(event, "_cd_original_user_text_v1", "") or "").strip()[:1000]
    if (
        isinstance(existing, JointReplyPrefetch)
        and existing.plugin is plugin
        and existing.event is event
        and existing.query == query
    ):
        return existing
    handle = JointReplyPrefetch(plugin, event, query)
    event._stealer_joint_prefetch_v1 = handle
    return handle
