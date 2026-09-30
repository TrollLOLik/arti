"""Task-local transport scope for commands and direct media handlers."""
from telegram.ext import BaseUpdateProcessor
from cognition.runtime import CURRENT_TURN,get_runtime


class CognitiveUpdateProcessor(BaseUpdateProcessor):
    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self,update,coroutine):
        token = CURRENT_TURN.set(None)
        try:
            runtime = get_runtime()
            message = getattr(update,'message',None)
            user = getattr(update,'effective_user',None)
            chat = getattr(update,'effective_chat',None)
            # Voice/video require transcription first. Their handler registers
            # that observation; a placeholder must not replace its exact text.
            text = getattr(message,'text',None) or getattr(message,'caption',None)
            if text and text.split()[0].split('@')[0] in ('/forget','/clear_context','/rp','/charge','/my_profile','/memory_archive','/stop','/start','/cancel'):
                # Control/diagnostic requests may contain the very topic being
                # erased. They are not new autobiographical evidence.
                text = None
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
