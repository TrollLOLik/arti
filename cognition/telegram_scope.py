"""Task-local transport scope for commands and direct media handlers."""
from telegram.ext import BaseUpdateProcessor
from cognition.runtime import CURRENT_TURN,get_runtime
from cognition.scope import CURRENT_SCOPE,from_update
from dataclasses import replace


class CognitiveUpdateProcessor(BaseUpdateProcessor):
    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self,update,coroutine):
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
            if text and text.split()[0].split('@')[0] in ('/forget','/clear_context','/rp','/charge','/my_profile','/memory_archive','/stop','/start','/cancel','/proactivity','/quiet'):
                # Control/diagnostic requests may contain the very topic being
                # erased. They are not new autobiographical evidence.
                text = None
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
