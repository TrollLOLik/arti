"""Deterministic Telegram HTML parts, each with its own durable send ordinal."""
import html
import re
from html.parser import HTMLParser


class _TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = {'strong':'b', 'em':'i', 'ins':'u', 'strike':'s', 'del':'s'}.get(tag, tag)
        if tag not in ('b','i','u','s','code','pre','a','tg-spoiler','blockquote'):
            return
        attrs = dict(attrs)
        active = [name for name, enabled in self.stack if enabled]
        # Telegram forbids formatting entities inside code/pre, nested links,
        # and nested blockquotes. Drop only the invalid formatting, never text.
        enabled = not (
            ('code' in active or 'pre' in active) and not (tag == 'code' and active == ['pre'])
            or tag in ('pre','code') and active and not (tag == 'code' and active == ['pre'])
            or tag in ('a','blockquote') and tag in active
            or len(active) >= 16
        )
        opening = '<' + tag + '>'
        if tag == 'a':
            href = attrs.get('href') or ''
            if len(href)>2048 or not re.match(r'^(?:https?://|tg://|mailto:)', href, re.I):
                enabled = False
            opening = '<a href="' + html.escape(href, quote=True) + '">'
        elif tag == 'code' and active == ['pre'] and re.fullmatch(r'language-[\w+-]{1,40}', attrs.get('class') or ''):
            opening = '<code class="' + attrs['class'] + '">'
        self.stack.append((tag, enabled))
        if enabled:
            self.tokens.append(('open', tag, opening))

    def handle_endtag(self, tag):
        tag = {'strong':'b', 'em':'i', 'ins':'u', 'strike':'s', 'del':'s'}.get(tag, tag)
        if tag in [name for name, _ in self.stack]:
            while self.stack:
                closing, enabled = self.stack.pop()
                if enabled:
                    self.tokens.append(('close', closing, '</' + closing + '>'))
                if closing == tag:
                    break

    def handle_data(self, data):
        self.tokens.append(('text', '', data))


def html_chunks(text, limit=4000):
    """Split after entity parsing; never split a code point, tag or entity.

    Telegram's UTF-16 convention is used conservatively for astral Unicode.
    Unsupported markup is removed and literal '<'/'&' safely escaped.
    """
    if not 2 <= limit <= 4096:
        raise ValueError('invalid_telegram_chunk_limit')
    parser = _TelegramHTML()
    parser.feed(text or '')
    parser.close()
    stack, parts, current, units = [], [], [], 0

    def flush():
        nonlocal current, units
        if units:
            parts.append(''.join(current) + ''.join('</'+tag+'>' for tag, _ in reversed(stack)))
        current = [opening for _, opening in stack]
        units = 0

    for kind, tag, content in parser.tokens:
        if kind == 'open':
            stack.append((tag, content))
            current.append(content)
        elif kind == 'close':
            if stack and stack[-1][0] == tag:
                stack.pop()
                current.append(content)
        else:
            for char in content:
                size = 2 if ord(char)>0xffff else 1
                if units + size > limit:
                    flush()
                current.append(html.escape(char, quote=False))
                units += size
    flush()
    return parts


async def send_html_reply(bot, *, chat_id, text, reply_to_message_id=None, reply_markup=None):
    from bot.request_runtime import checkpoint
    async def plan():
        return html_chunks(text) or ['Не удалось подготовить текст ответа. Отправь запрос ещё раз.']
    # A stored plan protects part identities even after code/format changes.
    parts = await checkpoint('reply_html_parts', plan)
    result = None
    for index, part in enumerate(parts):
        result = await bot.send_message(chat_id=chat_id, text=part, parse_mode='HTML',
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup if index == len(parts)-1 else None)
    return result
