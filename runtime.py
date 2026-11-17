"""Shared configuration, host ownership and lifecycle for the integrated engines."""

from __future__ import annotations

import threading

from astrbot.api.star import Star


class ConfigSection(dict):
    """Expose one settings section without overwriting its sibling on save."""

    def __init__(self, parent, key, lock=None):
        super().__init__(parent.get(key, {}))
        self.parent = parent
        self.key = key
        self.lock = lock or threading.RLock()
        self._revision = 0

    @property
    def _save_revision(self):
        return getattr(self.parent, "_save_revision", self._revision)

    def save_config(self, updates=None):
        with self.lock:
            before = dict(self)
            if updates is not None:
                self.update(updates)
            previous = self.parent.get(self.key)
            self.parent[self.key] = dict(self)
            try:
                if hasattr(self.parent, "save_config"):
                    self.parent.save_config()
                self._revision += 1
            except BaseException:
                self.parent[self.key] = previous
                self.clear()
                self.update(before)
                raise


class EngineContext:
    """Attribute API routes and manually registered tools to the single owner."""

    def __init__(self, owner):
        self.owner = owner
        self.host = owner.context
        self.routes = []
        self.tools = []

    def __getattr__(self, name):
        return getattr(self.host, name)

    def register_web_api(self, route, handler, methods, description):
        self.host.register_web_api(route, handler, methods, description)
        self.routes.append((route, handler, methods, description))

    def add_llm_tools(self, *tools):
        self.host.add_llm_tools(*tools)
        for tool in tools:
            tool.handler_module_path = self.owner.__class__.__module__
            self.tools.append(tool)

    def release(self):
        registered = self.host.registered_web_apis
        registered[:] = [r for r in registered if r not in self.routes]
        manager = getattr(
            getattr(self.host, "provider_manager", None), "llm_tools", None
        )
        if manager is not None:
            manager.func_list[:] = [
                t
                for t in manager.func_list
                if all(t is not owned for owned in self.tools)
            ]
        self.routes.clear()
        self.tools.clear()


class Component:
    """Use owner facilities without creating a second AstrBot plugin registration."""

    def __init__(self, context):
        self.context = context
        self.owner = context.owner
        self.name = self.owner.name
        self.author = self.owner.author
        self.plugin_id = self.owner.plugin_id
        self.logger = self.owner.logger

    async def get_kv_data(self, key, default=None):
        return await self.owner.get_kv_data(key, default)

    async def put_kv_data(self, key, value):
        return await self.owner.put_kv_data(key, value)

    html_render = Star.html_render
    text_to_image = Star.text_to_image

    async def initialize(self):
        pass

    async def terminate(self):
        pass
