from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl import background_notify as notify


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_background_agent_receives_host_context(monkeypatch, wrapped):
    host = SimpleNamespace(
        get_config=lambda **kwargs: {},
        conversation_manager=object(),
        get_llm_tool_manager=lambda: SimpleNamespace(get_builtin_tool=lambda cls: object()),
    )

    class Proxy:
        def __init__(self):
            self.host = host

        def __getattr__(self, name):
            return getattr(self.host, name)

    class CronEvent:
        def __init__(self, *, context, **kwargs):
            assert context is host
            self._has_send_oper = False

    async def get_conversation(*, event, plugin_context):
        assert plugin_context is host
        return SimpleNamespace(history="[]")

    async def build_agent(*, event, plugin_context, **kwargs):
        assert plugin_context is host

        async def steps(limit):
            event._has_send_oper = True
            yield None

        return SimpleNamespace(agent_runner=SimpleNamespace(step_until_done=steps))

    monkeypatch.setattr(notify, "_OFFICIAL_API_READY", True)
    monkeypatch.setattr(notify, "MessageSession", SimpleNamespace(from_str=lambda umo: SimpleNamespace(message_type="group")))
    monkeypatch.setattr(notify, "CronMessageEvent", CronEvent)
    monkeypatch.setattr(notify, "MainAgentBuildConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(notify, "get_session_tool_timeout", lambda *args: 60)
    monkeypatch.setattr(notify, "_get_session_conv", get_conversation)
    monkeypatch.setattr(notify, "build_main_agent", build_agent)
    monkeypatch.setattr(notify, "BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT", "{background_task_result}")
    monkeypatch.setattr(notify, "ProviderRequest", lambda: SimpleNamespace(system_prompt="", func_tool=None))
    monkeypatch.setattr(notify, "ToolSet", lambda: SimpleNamespace(add_tool=lambda tool: None))
    monkeypatch.setattr(notify, "persist_agent_history", AsyncMock())
    monkeypatch.setattr(notify, "_bump_delivery_count", AsyncMock())
    plugin = SimpleNamespace(context=Proxy() if wrapped else host)
    event = SimpleNamespace(unified_msg_origin="test:GroupMessage:1", role="member")
    assert await notify.notify_llm_background_result(plugin, event, {"result": "done"}, notice="done", scene="test")
    notify.persist_agent_history.assert_awaited_once()
