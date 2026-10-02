"""Edit navigation in place. Never turn an ambiguous edit into a new message."""
import uuid
from hashlib import sha256
from types import SimpleNamespace
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter
from bot.retry_bot import RetryBot, _maybe_record_sent
from materials.types import canonical, MaterialError


def bounded_html(text):
    if len(text)<=3600:
        return text
    import re
    from html import unescape,escape
    # Long legacy summaries are safely shortened as plain text; never cut an
    # HTML entity or leave an open tag that makes the whole panel uneditable.
    plain=unescape(re.sub(r'<[^>]*>','',text))
    result=''
    for char in plain:
        piece=escape(char)
        if len(result)+len(piece)>3500:
            break
        result+=piece
    return result+'\n…'


def native(bot, name):
    return getattr(super(RetryBot, bot), name) if isinstance(bot, RetryBot) else getattr(bot, name)


class Panel:
    def __init__(self, store, row, bot):
        self.store, self.row, self.bot = store, row, bot
        self.output = False

    async def render(self, text, buttons=(), *, screen=None, state=None, allow_create=False):
        # Digest includes semantic actions, not random callback tokens.
        text = bounded_html(text)
        digest = sha256(canonical([text, buttons]).encode()).hexdigest()
        updates = {}
        if screen is not None:
            updates['screen'] = screen
        if state is not None:
            updates['state'] = state
        if self.row['content_hash'] == digest and self.row['status'] == 'active':
            if updates:
                await self.store.save(self.row, **updates)
            self.output = True
            return self.receipt(text)
        actions, keyboard = {}, []
        for line in buttons:
            rendered = []
            for label, action in line:
                if 'url' in action:
                    rendered.append(InlineKeyboardButton(label[:64], url=action['url']))
                    continue
                token = uuid.uuid4().hex[:24]
                actions[token] = action
                rendered.append(InlineKeyboardButton(label[:64], callback_data='menu:'+self.row['id']+':'+token))
            keyboard.append(rendered)
        markup = InlineKeyboardMarkup(keyboard)
        await self.store.save(self.row, **updates, actions=actions, content_hash=None,
                              revision=self.row['revision']+1)
        kwargs = dict(chat_id=self.row['chat_id'], text=text, parse_mode='HTML', reply_markup=markup)
        try:
            if self.row['message_id'] is not None:
                result = await native(self.bot, 'edit_message_text')(
                    message_id=self.row['message_id'], **kwargs)
            else:
                if not allow_create or self.row['status'] == 'sending':
                    raise MaterialError('menu_no_confirmed_message')
                await self.store.save(self.row, status='sending')
                if self.row['topic_id'] > 0:
                    kwargs['message_thread_id'] = self.row['topic_id']
                result = await native(self.bot, 'send_message')(**kwargs)
                if type(getattr(result, 'message_id', None)) is not int:
                    raise MaterialError('menu_send_unknown')
                await self.store.save(self.row, message_id=result.message_id)
            await self.store.save(self.row, status='active', content_hash=digest)
            self.output = True
            _maybe_record_sent(result)
            return result if getattr(result, 'message_id', None) else self.receipt(text)
        except BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                await self.store.save(self.row, status='active', content_hash=digest)
                self.output = True
                return self.receipt(text)
            # Only a definitive missing message permits replacement on explicit /menu.
            missing = any(x in str(exc).lower() for x in ('message to edit not found', "message can't be edited"))
            await self.store.save(self.row, status='gone' if missing else 'unknown')
            raise
        except (NetworkError, RetryAfter, MaterialError):
            await self.store.save(self.row, status='unknown')
            raise

    def receipt(self, text=''):
        return SimpleNamespace(message_id=self.row['message_id'], text=text,
            chat=SimpleNamespace(id=self.row['chat_id']), delete=self.no_delete)

    async def no_delete(self, *args, **kwargs):
        # Legacy input cleanup must not delete the shared navigation panel.
        return False

    def navigation(self, back='home'):
        return [[('‹ Назад', dict(nav=back)), ('⌂ Меню', dict(nav='home'))]]
