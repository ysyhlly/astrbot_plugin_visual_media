"""Request-owned candidate search on a detached event; no send or injection."""
import asyncio
import copy
import time


class JointReplyPrefetch:
    def __init__(self,plugin,event,query):
        self.plugin,self.event,self.query=plugin,event,query
        self.started=time.monotonic()
        self.task=asyncio.create_task(self._search())
        jobs=getattr(plugin,'_joint_reply_prefetches',None)
        if jobs is None:
            jobs=set();plugin._joint_reply_prefetches=jobs
        jobs.add(self)
        def done(task):
            jobs.discard(self)
            if not task.cancelled():task.exception()
        self.task.add_done_callback(done)

    async def _search(self):
        clone=copy.copy(self.event)
        clone._extras=dict(getattr(self.event,'_extras',{}) or {})
        clone._extras.pop('provider_request',None)
        for name in ('_cd_deferred_reply_media_v1','_stealer_passive_delivery_v1','_stealer_joint_meme_plan_v1'):
            if hasattr(clone,name):delattr(clone,name)
        clone._emoji_turn_state={}
        clone._result=None
        clone._temporary_local_files=[]
        async def no_send(*args,**kwargs):
            raise RuntimeError('prefetch_cannot_send')
        clone.send=no_send
        async def run():
            async for _ in self.plugin._search_meme_impl(clone,self.query):
                pass
            return [dict(row) for row in self.plugin._emoji_sender_engine.emoji_turn_state(clone).get_candidates()[:5]]
        if not self.query or not self.plugin.is_send_enabled_for_event(self.event):
            return []
        return await asyncio.wait_for(run(),timeout=6)

    async def result(self,plugin,event,query,*,wait_seconds=None):
        if (self.plugin is not plugin or self.event is not event or self.query!=query
                or getattr(plugin,'_terminated',False) is True):
            return None
        try:
            result=(await self.task if wait_seconds is None else
                    await asyncio.wait_for(asyncio.shield(self.task),timeout=wait_seconds))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return []
        except Exception:
            return []
        if not plugin.is_send_enabled_for_event(event):
            return []
        return [dict(row) for row in result
                if plugin.meme_selector.is_path_allowed_for_event(row['path'],event)]

    async def close(self):
        if not self.task.done():self.task.cancel()
        await asyncio.gather(self.task,return_exceptions=True)


def start_joint_prefetch(plugin,event):
    config=plugin.plugin_config
    if (not getattr(config,'llm_joint_reply',True) or not getattr(config,'auto_send_meme',True)
            or getattr(event,'_cd_joint_meme_supported_v1',False) is not True
            or plugin._emoji_sender_engine.is_command_event(event)
            or not plugin.is_send_enabled_for_event(event)
            or getattr(plugin,'_terminated',False) is True):
        return None
    existing=getattr(event,'_stealer_joint_prefetch_v1',None)
    query=str(getattr(event,'_cd_original_user_text_v1','') or '').strip()[:1000]
    if isinstance(existing,JointReplyPrefetch) and existing.plugin is plugin and existing.event is event and existing.query==query:
        return existing
    handle=JointReplyPrefetch(plugin,event,query)
    event._stealer_joint_prefetch_v1=handle
    return handle
