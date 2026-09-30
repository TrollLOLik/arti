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


async def _save_cognitive_history(chat_id,user_name,text,user_id,message_id,mode,occurred_at):
    from cognition.runtime import get_runtime
    runtime = get_runtime()
    if not runtime or runtime.mode=='legacy' or message_id is None:
        return False
    from cognition.scope import CURRENT_SCOPE
    scope=CURRENT_SCOPE.get()
    if scope and scope.group and scope.chat_id==chat_id:
        if user_name=='Арти' or scope.sender_kind=='bot' or scope.topic_id<0: return True
        from dataclasses import replace
        from cognition.serialization import load_event
        cid=await runtime.groups.observe(replace(scope,message_id=message_id),text,mode,occurred_at)
        async with runtime.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT e.id,e.payload FROM cognitive_events e JOIN group_observations o ON o.event_id=e.id WHERE o.context_id=$1 AND o.message_id=$2 AND e.suppressed_at IS NULL',cid,message_id)
        if not row: return True
        eid=row['id']; event=load_event(row['payload'])
    else:
        if user_id is None: return False
        cid,eid,event = await runtime.ingest(chat_id,user_id,text,message_id,mode,occurred_at)
    from cognition.history import save_source_history,invalidate_history
    async with runtime.pool.acquire() as conn,conn.transaction():
        await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
        await save_source_history(conn,cid,eid,event,user_name,event.text,message_id)
    invalidate_history(chat_id)
    return True


async def save_chat_message(chat_id: int, user_name: str, message_text: str, user_id: int = None, *, message_id=None,occurred_at=None) -> None:
    """
    Унифицированная функция, которая:
    1) Сохраняет сообщение (с датой) в chat_history (до 30 сообщений).
    2) Инвалидирует кеш для этого чата.
    """
    if await _save_cognitive_history(chat_id,user_name,message_text,user_id,message_id,'default',occurred_at):
        return
    try:
        timestamp = datetime.now()

        # Сохраняем в базу данных (только в chat_history)
        await ChatHistoryModel.save(chat_id, user_name, message_text, timestamp)
        memory_id = await MemoryMessage.save(
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


async def _group_history(chat_id,mode):
    from cognition.scope import CURRENT_SCOPE
    from cognition.runtime import get_runtime
    scope=CURRENT_SCOPE.get(); runtime=get_runtime()
    if scope and scope.group and scope.chat_id==chat_id:
        return await runtime.groups.history(scope,mode) if runtime else ''
    return None


async def get_chat_context(chat_id, limit=20) -> str:
    """
    Получает последние сообщения (с датами) из истории чата, форматируя их для модели.
    Использует кеширование для оптимизации производительности.
    """
    group=await _group_history(chat_id,'default')
    if group is not None: return group
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


async def save_chat_message_rp(chat_id: int, user_name: str, message_text: str, user_id: int = None, *, message_id=None,occurred_at=None) -> None:
    """Сохраняет сообщение в RP-историю чата (до 30 сообщений) и инвалидирует кеш."""
    if await _save_cognitive_history(chat_id,user_name,message_text,user_id,message_id,'rp',occurred_at):
        return
    try:
        timestamp = datetime.now()
        await ChatHistoryRPModel.save(chat_id, user_name, message_text, timestamp)
        memory_id = await MemoryMessage.save(
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
    group=await _group_history(chat_id,'rp')
    if group is not None: return group
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


async def get_dialog_history_as_text(chat_id, limit=20) -> str:
    """
    Возвращает историю диалога (без дат) для данного chat_id в виде строки.
    Использует chat_history.
    """
    group=await _group_history(chat_id,'default')
    if group is not None: return group
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
    group=await _group_history(chat_id,'rp')
    if group is not None: return group
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

