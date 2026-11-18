"""Worker-thread persistence, with memory-only gates for synchronous CD callbacks."""

import asyncio
import time
from collections import OrderedDict
from threading import RLock
from types import SimpleNamespace

from astrbot.api import logger

from .media_behavior import MediaBehavior


class MediaRuntime(MediaBehavior):
    def __init__(self, root, settings=None):
        super().__init__(root, settings)
        self._hot = OrderedDict()
        self._hot_lock = RLock()
        self._leases = {}
        self._audit_tasks = set()
        self._closing = False
        self.stop_sends = False
        self.dropped_records = 0

    def prepare_sync(self, event, candidates):
        self.bind(event)
        filtered = self.filter_candidates(event, candidates)
        prefs, rows = self.snapshot(event)
        scope, member = self.identity(event)
        prepared = [{**c, "_vm_fingerprint": self.fingerprint(c)} for c in filtered]
        with self._hot_lock:
            previous = self._hot.get(scope, {"members": {}, "rows": []})
            merged = {(r["trace"], r["digest"]): r for r in rows + previous["rows"]}
            previous["rows"] = sorted(
                merged.values(), key=lambda r: r["created"], reverse=True
            )[: self.settings.recent_receipts]
            previous["members"][member] = prefs
            # Only recent active members need a synchronous snapshot.
            if len(previous["members"]) > 256:
                previous["members"].pop(next(iter(previous["members"])))
            self._hot[scope] = previous
            self._hot.move_to_end(scope)
            while len(self._hot) > 512:
                self._hot.popitem(last=False)
        return prepared

    async def prepare_candidates(self, event, candidates):
        return await asyncio.to_thread(self.prepare_sync, event, candidates)

    def reason_prepared(self, event, candidate):
        scope, member = self.identity(event)
        with self._hot_lock:
            state = self._hot.get(scope)
            if (
                not state
                or member not in state["members"]
                or "_vm_fingerprint" not in candidate
            ):
                return "snapshot_unavailable"
            return self.reason_for(
                event,
                candidate["_vm_fingerprint"],
                state["members"][member],
                state["rows"],
            )

    def reserve_prepared(self, event, candidate):
        with self._hot_lock:
            if self._closing or self.stop_sends:
                return False
            if self.reason_prepared(event, candidate):
                return False
            now = time.monotonic()
            self._leases = {k: v for k, v in self._leases.items() if v[1] > now}
            key = (self.identity(event)[0], candidate["_vm_fingerprint"][0])
            existing = self._leases.get(key)
            if existing and existing[0] != event._visual_media_trace:
                return False
            self._leases[key] = (event._visual_media_trace, now + 60)
            return True

    def release_prepared(self, event, candidate):
        with self._hot_lock:
            key = (self.identity(event)[0], candidate["_vm_fingerprint"][0])
            if self._leases.get(key, (None,))[0] == event._visual_media_trace:
                self._leases.pop(key, None)

    async def adelivered(self, event, candidate):
        self.bind(event)
        fingerprint = candidate.get("_vm_fingerprint") or await asyncio.to_thread(
            self.fingerprint, candidate
        )
        scope, _ = self.identity(event)
        with self._hot_lock:
            state = self._hot.get(scope)
            if state:
                state["rows"].insert(
                    0,
                    {
                        "digest": fingerprint[0],
                        "phash": fingerprint[1],
                        "caption": fingerprint[2],
                        "trace": event._visual_media_trace,
                        "created": time.time(),
                    },
                )
                state["rows"] = state["rows"][: self.settings.recent_receipts]
        if await asyncio.to_thread(self.delivered, event, candidate) is False:
            self.dropped_records += 1
        if "_vm_fingerprint" in candidate:
            self.release_prepared(event, candidate)

    async def arecord(self, event, *args, **kwargs):
        self.bind(event)
        if await asyncio.to_thread(self.record, event, *args, **kwargs) is False:
            self.dropped_records += 1

    def queue_record(self, event, *args, **kwargs):
        self.bind(event)
        if not self.settings.decision_records_enabled:
            return
        if self._closing or len(self._audit_tasks) >= 128:
            self.dropped_records += 1
            logger.warning("[VisualMedia] audit queue full or closing; record skipped")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self.record(event, *args, **kwargs) is False:
                self.dropped_records += 1
            return
        try:
            sender = event.get_sender_id()
        except AttributeError:
            sender = ""
        frozen = SimpleNamespace(
            unified_msg_origin=event.unified_msg_origin,
            get_sender_id=lambda: sender,
            _visual_media_trace=event._visual_media_trace,
        )
        task = loop.create_task(asyncio.to_thread(self.record, frozen, *args, **kwargs))
        self._audit_tasks.add(task)

        def completed(task):
            self._audit_tasks.discard(task)
            if task.cancelled() or task.exception() or task.result() is False:
                self.dropped_records += 1
                logger.warning("[VisualMedia] audit record failed")

        task.add_done_callback(completed)

    async def reset_async(self, scope, member):
        await asyncio.to_thread(self.reset, scope, member)
        with self._hot_lock:
            if scope in self._hot:
                self._hot[scope]["members"][member] = {
                    "frequency": "auto",
                    "style": "",
                    "blocked": [],
                }

    async def close(self):
        self._closing = True
        if self._audit_tasks:
            await asyncio.gather(*list(self._audit_tasks), return_exceptions=True)
