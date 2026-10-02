"""Task-local transport scope for commands and direct media handlers."""
import asyncio
import contextvars
from telegram.ext import BaseUpdateProcessor
from cognition.runtime import CURRENT_TURN,get_runtime
from cognition.scope import CURRENT_SCOPE,from_update
from dataclasses import replace


CURRENT_INTAKE_PROCESSOR = contextvars.ContextVar('arti_intake_processor', default=None)


async def cancel_pending_intake(chat_id, topic_id, owner_id=None):
    processor = CURRENT_INTAKE_PROCESSOR.get()
    if processor is None:
        return 0
    current = asyncio.current_task()
    tasks = [task for task, scope in tuple(processor._intakes.items())
             if task is not current and scope.chat_id == chat_id and (topic_id is None or scope.topic_id == topic_id)
             and (owner_id is None or scope.user_id == owner_id)]
    for task in tasks:
        task.cancel()
    if tasks:
        from utils.async_cleanup import await_owned
        await await_owned(asyncio.gather(*tasks, return_exceptions=True))
    return len(tasks)


class _ControlProcessor(BaseUpdateProcessor):
    def __init__(self, owner):
        super().__init__(4)
        self.owner = owner

    async def initialize(self): pass
    async def shutdown(self): pass
    async def do_process_update(self, update, coroutine):
        if update is None:
            await coroutine
        else:
            await self.owner._scoped_process_update(update, coroutine)


class CognitiveUpdateProcessor(BaseUpdateProcessor):
    """PTB adapter: lane admission precedes PTB's global work semaphore.

    PTB marks process_update typing.final, but its documented extension hook is
    inside the semaphore. Overriding that wrapper is intentional and covered by
    compatibility tests: delegation still awaits public super.process_update;
    no detached handler task, private semaphore mutation or provider work occurs
    during admission. Keep this contract tested when upgrading PTB.
    """
    MAX_PENDING_PER_LANE = 64
    MAX_PENDING = 512

    def __init__(self, max_concurrent_updates):
        super().__init__(max_concurrent_updates)
        self._lanes = {}
        self._intakes = {}
        self._controls = _ControlProcessor(self)

    async def initialize(self):
        await self._controls.initialize()

    async def shutdown(self):
        # PTB normally awaits process_update before shutdown; remain defensive.
        tasks = [task for task in self._intakes if task is not asyncio.current_task()]
        for task in tasks: task.cancel()
        if tasks:
            from utils.async_cleanup import await_owned
            await await_owned(asyncio.gather(*tasks, return_exceptions=True))
        await self._controls.shutdown()

    async def process_update(self, update, coroutine):
        runtime = get_runtime()
        scope = from_update(update, getattr(runtime,'bot_id',0), getattr(runtime,'bot_username',''))
        message = getattr(update,'effective_message',None)
        text = getattr(message,'text',None) or ''
        command = text.split()[0].split('@')[0] if text.split() else ''
        # Business handlers still authorize the operation. Callback/menu input
        # stays ordered; arbitrary text cannot acquire the control lane.
        control = command in ('/cancel','/stop','/request')
        token = CURRENT_INTAKE_PROCESSOR.set(self)
        task = asyncio.current_task()
        lane = None
        key = (scope.chat_id, scope.topic_id) if scope else None
        try:
            if control:
                return await self._controls.process_update(update, coroutine)
            if key is None:
                return await super().process_update(update, coroutine)
            lane = self._lanes.get(key)
            if lane is None:
                lane = self._lanes[key] = {'lock':asyncio.Lock(), 'count':0}
            if lane['count'] >= self.MAX_PENDING_PER_LANE or len(self._intakes) >= self.MAX_PENDING:
                # This input has not been accepted. Explicit refusal is safer
                # than an unbounded in-memory backlog or an implied save.
                async def busy():
                    if message is not None:
                        async with asyncio.timeout(3):
                            await message.reply_text('Слишком много сообщений в подготовке. Это сообщение не сохранено; проверь /request и отправь его позже.')
                try:
                    notice = busy()
                    try:
                        await self._controls.process_update(None, notice)
                    finally:
                        notice.close()
                except Exception:
                    pass
                return
            lane['count'] += 1
            self._intakes[task] = scope
            try:
                # asyncio.Lock is FIFO; acquisition is queued before any menu,
                # database, transcription or model await can overtake this input.
                async with lane['lock']:
                    return await super().process_update(update, coroutine)
            finally:
                self._intakes.pop(task, None)
                lane['count'] -= 1
        finally:
            if lane is not None and lane['count'] == 0:
                self._lanes.pop(key, None)
            if hasattr(coroutine, 'close'):
                coroutine.close()
            CURRENT_INTAKE_PROCESSOR.reset(token)

    async def do_process_update(self, update, coroutine):
        return await self._scoped_process_update(update, coroutine)

    async def _scoped_process_update(self,update,coroutine):
        token = CURRENT_TURN.set(None)
        runtime = get_runtime()
        scope = from_update(update,getattr(runtime,'bot_id',0),getattr(runtime,'bot_username',''))
        scope_token = CURRENT_SCOPE.set(scope)
        try:
            message = getattr(update,'effective_message',None) or getattr(update,'message',None) or getattr(update,'edited_message',None)
            user = getattr(update,'effective_user',None)
            chat = getattr(update,'effective_chat',None)
            # Voice/video require transcription first. Their handler registers
            # that observation; a placeholder must not replace its exact text.
            text = getattr(message,'text',None) or getattr(message,'caption',None)
            # Callback text belongs to the bot's panel, not the human clicking it.
            # Menu inputs are control data; business handlers retain actual consent.
            from bot.menu import is_menu_input
            menu_input = await is_menu_input(update,getattr(runtime,'pool',None))
            if getattr(update,'callback_query',None) or menu_input:
                text = None
            if text and text.split()[0].split('@')[0] in ('/menu','/arti_commands','/forget','/clear_context','/rp','/charge','/my_profile','/memory_archive','/stop','/start','/cancel','/proactivity','/quiet','/organizer','/todo','/event','/remind','/timezone','/request'):
                # Control/diagnostic requests may contain the very topic being
                # erased. They are not new autobiographical evidence.
                text = None
            if runtime and scope and scope.chat_type=='private' and text and user and message:
                from organizer.natural import claim_input
                from config import rp_mode_state
                await claim_input(runtime.pool,user.id,scope.chat_id,message.message_id,text,mode='rp' if rp_mode_state.get(scope.chat_id) else 'default')
            if runtime and runtime.mode!='legacy' and scope and scope.group and message:
                migrated_to=getattr(message,'migrate_to_chat_id',None)
                migrated_from=getattr(message,'migrate_from_chat_id',None)
                if migrated_to: await runtime.groups.migrate_chat(scope.chat_id,migrated_to)
                if migrated_from: await runtime.groups.migrate_chat(migrated_from,scope.chat_id)
                if getattr(message,'forum_topic_closed',None): await runtime.groups.set_closed(scope,True)
                if getattr(message,'forum_topic_reopened',None): await runtime.groups.set_closed(scope,False)
            if runtime and runtime.mode!='legacy' and scope and scope.group and text and not text.startswith('/'):
                from config import rp_mode_state
                from utils.response_status import is_responses_enabled
                if await is_responses_enabled(scope.chat_id):
                    mode='rp' if rp_mode_state.get(scope.chat_id) else 'default'
                    edited=bool(getattr(update,'edited_message',None))
                    if edited: scope=replace(scope,addressed=False)
                    elif not scope.addressed and await runtime.groups.continuation(scope,text,mode): scope=replace(scope,addressed=True)
                    CURRENT_SCOPE.set(scope)
                    await runtime.groups.observe(scope,text,mode,getattr(message,'edit_date',None) if edited else getattr(message,'date',None),message,edited)
            if runtime and runtime.mode!='legacy' and message and user and chat and text and text.startswith('/'):
                from config import rp_mode_state
                from utils.response_status import is_responses_enabled
                if await is_responses_enabled(chat.id):
                    mode = 'rp' if rp_mode_state.get(chat.id) else 'default'
                    turn = await runtime.prepare(chat.id,user.id,text,message.message_id,mode,task_serious=text.startswith('/'))
                    if turn.active and turn.repeated_delivery:
                        if hasattr(coroutine,'close'):
                            coroutine.close()
                        return
            await coroutine
        finally:
            CURRENT_TURN.reset(token)
            CURRENT_SCOPE.reset(scope_token)
