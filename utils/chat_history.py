"""
Управление историей чатов (с использованием PostgreSQL)
"""
import logging
from datetime import datetime, timedelta
import asyncio

from database.models import (
    ChatHistory as ChatHistoryModel,
    ChatHistoryRP as ChatHistoryRPModel,
    MemoryMessage,
)

logger = logging.getLogger(__name__)

# Кеш для контекста чата и недавних сообщений (оптимизация)
_context_cache = {}  # {chat_id: (context_str, timestamp)}
_recent_messages_cache = {}  # {chat_id: (messages_list, timestamp)}
_dialog_history_cache = {}  # {chat_id: (history_str, timestamp)}
_cache_ttl = timedelta(seconds=5)  # TTL кеша - 5 секунд

# Кеши для RP-режима
_context_cache_rp = {}  # {chat_id: (context_str, timestamp)}
_recent_messages_cache_rp = {}  # {chat_id: (messages_list, timestamp)}
_dialog_history_cache_rp = {}  # {chat_id: (history_str, timestamp)}


def _memory_role(user_name: str) -> str:
    if user_name == "Арти":
        return "assistant"
    if user_name == "Память":
        return "memory"
    return "user"


async def save_chat_message(chat_id: int, user_name: str, message_text: str, user_id: int = None) -> None:
    """
    Унифицированная функция, которая:
    1) Сохраняет сообщение (с датой) в chat_history (до 30 сообщений).
    2) Инвалидирует кеш для этого чата.
    
    Примечание: dialog_history больше не используется - всё хранится в chat_history
    """
    try:
        timestamp = datetime.now()

        # Сохраняем в базу данных (только в chat_history)
        await ChatHistoryModel.save(chat_id, user_name, message_text, timestamp)
        await MemoryMessage.save(
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            role=_memory_role(user_name),
            mode="default",
            source="chat_history",
            message_text=message_text,
        )
        
        # Инвалидируем кеш при сохранении нового сообщения
        _context_cache.pop(chat_id, None)
        _recent_messages_cache.pop(chat_id, None)
        _dialog_history_cache.pop(chat_id, None)
    except Exception as e:
        logger.error(f"Ошибка при сохранении сообщения в БД: {e}", exc_info=True)


# Синхронная обертка для обратной совместимости
def save_chat_message_sync(chat_id: int, user_name: str, message_text: str, user_id: int = None) -> None:
    """Синхронная обертка для save_chat_message"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(save_chat_message(chat_id, user_name, message_text, user_id=user_id))
        else:
            loop.run_until_complete(save_chat_message(chat_id, user_name, message_text, user_id=user_id))
    except RuntimeError:
        # Если нет event loop, создаем новый
        asyncio.run(save_chat_message(chat_id, user_name, message_text, user_id=user_id))


async def get_chat_context(chat_id, limit=20) -> str:
    """
    Получает последние сообщения (с датами) из истории чата, форматируя их для модели.
    Использует кеширование для оптимизации производительности.
    """
    try:
        now = datetime.now()
        
        # Проверяем кеш
        if chat_id in _context_cache:
            cached_context, cache_time = _context_cache[chat_id]
            if now - cache_time < _cache_ttl:
                return cached_context
        
        # Получаем сообщения из базы данных
        messages = await ChatHistoryModel.get_recent(chat_id, limit)

        context = ""
        for timestamp, message in messages:
            formatted_time = timestamp.strftime("%Y-%m-%d %H:%M:%S")
            context += f"[{formatted_time}] {message}\n"

        context_str = context.strip()
        
        # Сохраняем в кеш
        _context_cache[chat_id] = (context_str, now)
        
        return context_str
    except Exception as e:
        logger.error(f"Ошибка при получении истории чата: {e}", exc_info=True)
        return ""


# Синхронная обертка для обратной совместимости
def get_chat_context_sync(chat_id, limit=20) -> str:
    """Синхронная обертка для get_chat_context"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return ""
        else:
            return loop.run_until_complete(get_chat_context(chat_id, limit))
    except RuntimeError:
        return asyncio.run(get_chat_context(chat_id, limit))


async def save_chat_message_rp(chat_id: int, user_name: str, message_text: str, user_id: int = None) -> None:
    """Сохраняет сообщение в RP-историю чата (до 30 сообщений) и инвалидирует кеш."""
    try:
        timestamp = datetime.now()
        await ChatHistoryRPModel.save(chat_id, user_name, message_text, timestamp)
        await MemoryMessage.save(
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            role=_memory_role(user_name),
            mode="rp",
            source="chat_history",
            message_text=message_text,
        )
        _context_cache_rp.pop(chat_id, None)
        _recent_messages_cache_rp.pop(chat_id, None)
    except Exception as e:
        logger.error(f"Ошибка при сохранении RP-сообщения в БД: {e}", exc_info=True)


async def get_chat_context_rp(chat_id, limit=20) -> str:
    """Получает последние сообщения из RP-истории чата, форматируя их для модели."""
    try:
        now = datetime.now()
        if chat_id in _context_cache_rp:
            cached_context, cache_time = _context_cache_rp[chat_id]
            if now - cache_time < _cache_ttl:
                return cached_context

        messages = await ChatHistoryRPModel.get_recent(chat_id, limit)
        context = ""
        for timestamp, message in messages:
            formatted_time = timestamp.strftime("%Y-%m-%d %H:%M:%S")
            context += f"[{formatted_time}] {message}\n"

        context_str = context.strip()
        _context_cache_rp[chat_id] = (context_str, now)
        return context_str
    except Exception as e:
        logger.error(f"Ошибка при получении RP-истории чата: {e}", exc_info=True)
        return ""


async def get_recent_messages(chat_id, timeout) -> list:
    """
    Получает недавные сообщения за указанный период времени.
    Использует кеширование для оптимизации производительности.
    
    Args:
        chat_id: ID чата
        timeout: Таймаут в секундах (int или timedelta)
    
    Returns:
        List[Tuple[datetime, str]]: Список сообщений в формате (timestamp, message)
    """
    try:
        now = datetime.now()
        
        # Преобразуем timeout в timedelta, если это число
        if isinstance(timeout, (int, float)):
            timeout_delta = timedelta(seconds=timeout)
        else:
            timeout_delta = timeout
        
        # Проверяем кеш
        if chat_id in _recent_messages_cache:
            cached_messages, cache_time = _recent_messages_cache[chat_id]
            if now - cache_time < _cache_ttl:
                # Фильтруем по таймауту (может измениться между запросами)
                return [msg for msg in cached_messages if now - msg[0] <= timeout_delta]
        
        # Получаем сообщения из базы данных
        messages = await ChatHistoryModel.get_recent(chat_id, 100)  # Берем больше для фильтрации
        recent_messages = [
            msg for msg in messages
            if now - msg[0] <= timeout_delta
        ]
        
        # Сохраняем в кеш
        _recent_messages_cache[chat_id] = (recent_messages, now)
        
        return recent_messages
    except Exception as e:
        logger.error(f"Ошибка при получении недавних сообщений: {e}", exc_info=True)
        return []


# Синхронная обертка
def get_recent_messages_sync(chat_id, timeout) -> list:
    """Синхронная обертка для get_recent_messages"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return []
        else:
            return loop.run_until_complete(get_recent_messages(chat_id, timeout))
    except RuntimeError:
        return asyncio.run(get_recent_messages(chat_id, timeout))


async def get_dialog_history_as_text(chat_id, limit=20) -> str:
    """
    Возвращает историю диалога (без дат) для данного chat_id в виде строки.
    Использует chat_history вместо отдельной таблицы dialog_history.
    """
    try:
        now = datetime.now()
        
        # Проверяем кеш
        if chat_id in _dialog_history_cache:
            cached_history, cache_time = _dialog_history_cache[chat_id]
            if now - cache_time < _cache_ttl:
                return cached_history
        
        # Получаем сообщения из chat_history (без дат)
        messages = await ChatHistoryModel.get_recent(chat_id, limit)
        
        # Форматируем без дат: просто "user_name: message_text"
        history_lines = []
        for timestamp, message in messages:
            history_lines.append(message)
        
        history_str = "\n".join(history_lines)
        
        # Сохраняем в кеш
        _dialog_history_cache[chat_id] = (history_str, now)
        
        return history_str
    except Exception as e:
        logger.error(f"Ошибка при получении диалоговой истории: {e}", exc_info=True)
        return ""


async def get_dialog_history_as_text_rp(chat_id, limit=20) -> str:
    """Возвращает RP-историю диалога (без дат) для данного chat_id в виде строки."""
    try:
        now = datetime.now()
        if chat_id in _dialog_history_cache_rp:
            cached_history, cache_time = _dialog_history_cache_rp[chat_id]
            if now - cache_time < _cache_ttl:
                return cached_history

        messages = await ChatHistoryRPModel.get_recent(chat_id, limit)
        history_lines = []
        for timestamp, message in messages:
            history_lines.append(message)

        history_str = "\n".join(history_lines)
        _dialog_history_cache_rp[chat_id] = (history_str, now)
        return history_str
    except Exception as e:
        logger.error(f"Ошибка при получении RP-диалоговой истории: {e}", exc_info=True)
        return ""


# Синхронная обертка
def get_dialog_history_as_text_sync(chat_id) -> str:
    """Синхронная обертка для get_dialog_history_as_text"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return ""
        else:
            return loop.run_until_complete(get_dialog_history_as_text(chat_id))
    except RuntimeError:
        return asyncio.run(get_dialog_history_as_text(chat_id))

