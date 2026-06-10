"""
Обработка текста и вспомогательные функции для Telegram
"""
import re
import asyncio
import logging
from difflib import get_close_matches
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def contains_arti(text, threshold=0.7):
    """Проверяет, содержит ли текст упоминание 'арти' (с учётом нечёткого поиска)."""
    text_lower = text.lower()

    if re.search(r'\bарти\b', text_lower):
        return True

    words = re.findall(r'\b\w{3,5}\b', text_lower)

    for word in words:
        matches = get_close_matches(word, ["арти"], n=1, cutoff=threshold)
        if matches:
            return True

    if re.search(r'\b\w*арти\w*\b', text_lower):
        long_words = re.findall(r'\b\w{6,}\b', text_lower)
        for word in long_words:
            if 'арти' in word:
                if get_close_matches("арти", [word[:4]], cutoff=0.7):
                    return True
    return False


def fix_html_tags(text: str) -> str:
    """
    Исправляет незакрытые HTML-теги в тексте перед отправкой в Telegram.
    Telegram поддерживает: <b>, <i>, <u>, <s>, <code>, <pre>, <a href>, <tg-spoiler>, <blockquote>
    Удаляет неподдерживаемые теги.
    """
    if not text:
        return text

    # Telegram поддерживает только эти теги
    allowed_tags = {'b', 'i', 'u', 's', 'code', 'pre', 'a', 'tg-spoiler', 'blockquote'}
    tag_pattern = re.compile(r'<(/?)([a-z]+)(?:\s[^>]*?)?>', re.IGNORECASE)
    
    open_tags_stack = []
    result_parts = []
    last_pos = 0
    
    for match in tag_pattern.finditer(text):
        is_closing = match.group(1) == '/'
        tag_name = match.group(2).lower()
        
        # Если тег не поддерживается Telegram, пропускаем его (удаляем)
        if tag_name not in allowed_tags:
            result_parts.append(text[last_pos:match.start()])
            last_pos = match.end()
            continue
        
        result_parts.append(text[last_pos:match.start()])
        
        if is_closing:
            found_index = -1
            for i in range(len(open_tags_stack) - 1, -1, -1):
                if open_tags_stack[i] == tag_name:
                    found_index = i
                    break
            
            if found_index >= 0:
                while len(open_tags_stack) > found_index + 1:
                    tag_to_close = open_tags_stack.pop()
                    result_parts.append(f'</{tag_to_close}>')
                
                open_tags_stack.pop()
                result_parts.append(match.group(0))
            # Лишний закрывающий тег — игнорируем
        else:
            open_tags_stack.append(tag_name)
            result_parts.append(match.group(0))
        
        last_pos = match.end()
    
    result_parts.append(text[last_pos:])
    
    while open_tags_stack:
        tag = open_tags_stack.pop()
        result_parts.append(f'</{tag}>')
    
    return ''.join(result_parts)


def extract_urls_and_make_keyboard(text: str, extra_links=None):
    """
    Вырезает ссылки из текста и превращает их в Inline-кнопки.
    extra_links: список кортежей (uri, title) из метаданных.
    """
    url_pattern = r'(https?://[^\s)\]>]+)'
    urls_in_text = re.findall(url_pattern, text)
    
    url_titles = {}
    
    if extra_links:
        for uri, title in extra_links:
            url_titles[uri] = title

    for url in urls_in_text:
        url_clean = url.rstrip('.,;')
        if url_clean not in url_titles:
            try:
                domain = urlparse(url_clean).netloc.replace('www.', '')
            except:
                domain = "Ссылка"
            url_titles[url_clean] = domain
        
        text = text.replace(url, "")
    
    if not url_titles:
        return text, None
        
    text = re.sub(r'\(\s*[,.]?\s*\)', '', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text).strip()

    unique_urls = list(url_titles.keys())
    keyboard = []
    row = []
    
    for uri in unique_urls[:6]:
        title = url_titles[uri]
        if len(title) > 30:
            title = title[:27] + "..."
            
        row.append(InlineKeyboardButton(f"🔗 {title}", url=uri))
        if len(row) == 2:
            keyboard.append(row)
            row = []
            
    if row: keyboard.append(row)
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return text, reply_markup


async def send_continuous_action(bot, chat_id, action, interval=4):
    """Асинхронно отправляет действие (typing, upload_photo) каждые interval секунд."""
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except Exception as e:
                logger.debug(f"Ошибка при отправке chat action {action}: {e}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass


async def repeat_chat_action(bot, chat_id, action, interval=4):
    """Отправляет chat action каждые interval секунд, пока задача не отменена."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except Exception as e:
                logger.debug(f"Ошибка при отправке chat action {action}: {e}")
    except asyncio.CancelledError:
        return
