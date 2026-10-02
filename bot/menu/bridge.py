"""Reuse native handlers; route their navigation text into the current panel."""
import importlib
import shlex
from datetime import datetime, timezone
from html import escape
from types import SimpleNamespace
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup
from materials.types import MaterialError

HANDLERS = {
    'project': ('bot.project_commands', 'project_command'),
    'artifact': ('bot.artifact_commands', 'artifact_command'),
    'task': ('bot.artifact_commands', 'task_command'),
    'dataset': ('bot.table_commands', 'dataset_command'),
    'calc': ('bot.table_commands', 'calc_command'),
    'datafix': ('bot.table_commands', 'datafix_command'),
    'transcript': ('bot.audio_commands', 'transcript_command'),
    'listen': ('bot.audio_commands', 'listen_command'),
    'transcript_fix': ('bot.audio_commands', 'transcript_fix_command'),
    'moment': ('bot.video_commands', 'moment_command'),
    'storyboard': ('bot.video_commands', 'storyboard_command'),
    'materials_find': ('bot.material_search', 'material_search_command'),
    'material_review': ('bot.material_search', 'material_review_command'),
    'proactivity': ('bot.group_commands', 'proactivity_command'),
    'quiet': ('bot.group_commands', 'quiet_command'),
}
for key, name in dict(image='handle_image_command', video='handle_video_command', music='handle_music_command',
        dub='handle_dub_command', vclone='handle_vclone_command', voices='handle_voices_command',
        voice_save='handle_voice_save_command', voice_delete='handle_voice_delete_command',
        model='handle_model_command', my_profile='handle_my_profile_command', charge='handle_charge_command',
        memory_archive='handle_memory_archive_command', forget='handle_forget_command',
        rp='handle_rp_command', rps='handle_rps_command', start='start', stop='stop',
        clear_context='clear_context', cancel='handle_cancel_command').items():
    HANDLERS[key] = ('bot.commands', name)
for key in ('decision', 'assignment', 'procedure', 'subscription', 'scenario'):
    HANDLERS[key] = ('bot.workflow_commands', 'workflow_command')

CALLBACKS = [('work:', 'bot.work_cards', 'work_callback'),
    ('model_', 'bot.commands', 'model_callback'), ('rps_', 'bot.commands', 'rps_callback'),
    ('prof_', 'bot.commands', 'profile_callback'), ('forget_', 'bot.commands', 'forget_callback'),
    ('vclone_clean:', 'bot.commands', 'vclone_clean_callback'), ('vsave:', 'bot.commands', 'vclone_save_callback'),
    ('vsel:', 'bot.commands', 'saved_voice_callback'), ('vdel:', 'bot.commands', 'saved_voice_callback'),
    ('photo_act:', 'bot.handlers', 'photo_action_callback'), ('doc_act:', 'bot.handlers', 'document_action_callback'),
    ('vurl:', 'bot.handlers', 'video_url_action_callback')]


class BotSurface:
    def __init__(self, controller):
        self.controller = controller
        self.real_bot = controller.context.bot
        self._menu_panel = controller.panel

    def __getattr__(self, key):
        return getattr(self.real_bot, key)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id != self.controller.panel.row['chat_id']:
            raise MaterialError('menu_destination_changed')
        return await self.controller.capture(text, kwargs.get('reply_markup'), kwargs.get('parse_mode'))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        if chat_id not in (None, self.controller.panel.row['chat_id']):
            raise MaterialError('menu_destination_changed')
        return await self.controller.capture(text, kwargs.get('reply_markup'), kwargs.get('parse_mode'))

    async def delete_message(self, chat_id=None, message_id=None, **kwargs):
        return False


class MessageSurface:
    def __init__(self, controller, *, text=None, reply=None):
        self.controller = controller
        self.original = controller.update.effective_message
        self.text = text if text is not None else (getattr(self.original, 'text', None) or '')
        self.from_user = controller.update.effective_user
        self.chat = controller.update.effective_chat
        self.chat_id = self.chat.id
        self.message_id = self.original.message_id
        self.reply_to_message = reply
        query = getattr(controller.update, 'callback_query', None)
        self._menu_event_id = 'menu-callback:'+query.id if query else str(self.message_id)
        self._menu_request_source = f'telegram:{self.chat_id}:{self._menu_event_id}:user'
        self.caption = None
        self.date = getattr(self.original, 'date', datetime.now(timezone.utc))

    def __getattr__(self, key):
        return getattr(self.original, key)

    def get_bot(self):
        return self.controller.bot

    async def reply_text(self, text, **kwargs):
        return await self.controller.capture(text, kwargs.get('reply_markup'), kwargs.get('parse_mode'))

    async def edit_text(self, text, **kwargs):
        return await self.reply_text(text, **kwargs)

    async def delete(self, **kwargs):
        return False

    def __getattribute__(self, key):
        if key.startswith('reply_') and key not in ('reply_text', 'reply_to_message'):
            kind = key[6:]
            async def send(value=None, *args, **kwargs):
                c = object.__getattribute__(self, 'controller')
                field = 'document' if kind == 'document' else kind
                kwargs.setdefault(field, value)
                kwargs.setdefault('chat_id', self.chat_id)
                if c.panel.row['topic_id'] > 0:
                    kwargs['message_thread_id'] = c.panel.row['topic_id']
                return await getattr(c.context.bot, 'send_'+kind)(*args, **kwargs)
            return send
        return object.__getattribute__(self, key)


class QuerySurface:
    def __init__(self, controller, data, message):
        self.original = controller.update.callback_query
        self.message, self.data = message, data
        self.from_user = controller.update.effective_user

    def __getattr__(self, key):
        return getattr(self.original, key)

    async def answer(self, text=None, **kwargs):
        # Root callback already acknowledged; alerts may still explain refusal.
        if text:
            return await self.original.answer(text[:190], **kwargs)

    async def edit_message_text(self, text, **kwargs):
        return await self.message.reply_text(text, **kwargs)

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        return await self.message.reply_text(self.message.controller.last_text, reply_markup=reply_markup, parse_mode='HTML')

    async def delete_message(self, **kwargs):
        return False


def surfaces(controller, text=None, reply=None, callback=None):
    message = MessageSurface(controller, text=text, reply=reply)
    update = SimpleNamespace(message=message, effective_message=message, effective_user=controller.update.effective_user,
        effective_chat=controller.update.effective_chat, callback_query=None,
        update_id=getattr(controller.update, 'update_id', 0))
    if callback is not None:
        update.callback_query = QuerySurface(controller, callback, message)
    context = SimpleNamespace(bot=controller.bot, args=shlex.split(text)[1:] if text and text.startswith('/') else [],
        user_data=controller.context.user_data, chat_data=controller.context.chat_data,
        bot_data=controller.context.bot_data, application=controller.context.application,
        _menu_real_context=controller.context)
    return update, context


async def command(controller, text, reply=None):
    key = text.split()[0].split('@')[0].lstrip('/')
    module, name = HANDLERS[key]
    update, context = surfaces(controller, text, reply)
    await getattr(importlib.import_module(module), name)(update, context)


async def callback(controller, data):
    for prefix, module, name in CALLBACKS:
        if data.startswith(prefix):
            update, context = surfaces(controller, callback=data)
            await getattr(importlib.import_module(module), name)(update, context)
            return
    raise MaterialError('menu_callback_unknown')


def buttons_from_markup(markup):
    if isinstance(markup, InlineKeyboardMarkup):
        return [[(b.text, dict(legacy=b.callback_data)) if b.callback_data else
                 (b.text, dict(url=b.url)) for b in line if b.callback_data or b.url]
                for line in markup.inline_keyboard]
    if isinstance(markup, ReplyKeyboardMarkup):
        return [[(b.text, dict(legacy_text=b.text)) for b in line] for line in markup.keyboard]
    return []
