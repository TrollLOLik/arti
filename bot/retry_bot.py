"""
RetryBot — кастомный бот с автоматическими ретраями при сетевых ошибках.
"""
import asyncio
import logging
import random

from telegram.error import TimedOut, NetworkError
from telegram.ext import ExtBot

from utils.sent_messages import record_bot_message

logger = logging.getLogger(__name__)


def _maybe_record_sent(result) -> None:
    """Если результат вызова похож на отправленное сообщение — запоминаем его ID
    (AUTH-03: чтобы реагировать только на реакции к сообщениям самого бота)."""
    try:
        chat = getattr(result, "chat", None)
        message_id = getattr(result, "message_id", None)
        if chat is not None and message_id is not None:
            record_bot_message(getattr(chat, "id", None), message_id)
    except Exception:
        pass


class RetryBot(ExtBot):
    """Кастомный бот с автоматическими ретраями при сетевых ошибках."""

    MAX_ATTEMPTS = 3

    async def _call_with_retry(self, method, *args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                result = await method(*args, **kwargs)
                _maybe_record_sent(result)
                return result
            except (TimedOut, NetworkError) as e:
                last_exc = e
                if attempt < self.MAX_ATTEMPTS - 1:
                    # Экспоненциальный бэкоф с джиттером: 2с -> 4с
                    delay = (2.0 ** attempt) + random.uniform(0.0, 1.0)
                    logger.warning(
                        "Ошибка сети (%s) на %s. Попытка %d/%d. Ждём %.1fс...",
                        e, getattr(method, "__name__", "?"),
                        attempt + 1, self.MAX_ATTEMPTS, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        if last_exc:
            raise last_exc

    async def send_message(self, *args, **kwargs):
        return await self._call_with_retry(super().send_message, *args, **kwargs)

    async def send_photo(self, *args, **kwargs):
        return await self._call_with_retry(super().send_photo, *args, **kwargs)

    async def send_audio(self, *args, **kwargs):
        return await self._call_with_retry(super().send_audio, *args, **kwargs)

    async def send_video(self, *args, **kwargs):
        return await self._call_with_retry(super().send_video, *args, **kwargs)

    async def send_voice(self, *args, **kwargs):
        return await self._call_with_retry(super().send_voice, *args, **kwargs)

    async def send_document(self, *args, **kwargs):
        return await self._call_with_retry(super().send_document, *args, **kwargs)

    # --- Чтения ---
    # get_file/edit_*/copy/forward тоже бывают подвержены TimedOut на flaky-сети.
    async def get_file(self, *args, **kwargs):
        return await self._call_with_retry(super().get_file, *args, **kwargs)

    async def edit_message_text(self, *args, **kwargs):
        return await self._call_with_retry(super().edit_message_text, *args, **kwargs)

    async def edit_message_caption(self, *args, **kwargs):
        return await self._call_with_retry(super().edit_message_caption, *args, **kwargs)

    async def edit_message_reply_markup(self, *args, **kwargs):
        return await self._call_with_retry(super().edit_message_reply_markup, *args, **kwargs)

    async def delete_message(self, *args, **kwargs):
        return await self._call_with_retry(super().delete_message, *args, **kwargs)

    async def send_chat_action(self, *args, **kwargs):
        # Chat action — косметический статус, ретраить его не нужно, 
        # чтобы не задерживать отправку ответов при сетевых сбоях.
        return await super().send_chat_action(*args, **kwargs)

    async def copy_message(self, *args, **kwargs):
        return await self._call_with_retry(super().copy_message, *args, **kwargs)

    async def forward_message(self, *args, **kwargs):
        return await self._call_with_retry(super().forward_message, *args, **kwargs)
