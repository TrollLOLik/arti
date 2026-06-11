"""Лёгкий трекер ID сообщений, отправленных самим ботом, по чатам.

Telegram в апдейте ``message_reaction`` НЕ передаёт автора сообщения. Чтобы Арти
реагировала только на реакции к СВОИМ сообщениям (AUTH-03), бот запоминает ID
своих исходящих сообщений в ограниченном по размеру (LRU) хранилище в памяти.

Переживать рестарт не обязано: после рестарта реакции на сообщения, отправленные
до него, просто игнорируются — это безопасный дефолт (лучше пропустить реакцию,
чем засчитать реакцию на чужое сообщение).
"""
from collections import OrderedDict, deque
from typing import Optional, Tuple

# Сколько чатов держим (по последней активности) и сколько message_id на чат.
_MAX_CHATS = 2000
_MAX_PER_CHAT = 400

# chat_id -> (deque(message_id, FIFO), set(message_id) для O(1) проверки)
_sent: "OrderedDict[int, Tuple[deque, set]]" = OrderedDict()


def record_bot_message(chat_id: Optional[int], message_id: Optional[int]) -> None:
    """Запоминает, что бот отправил сообщение message_id в чат chat_id."""
    if chat_id is None or message_id is None:
        return

    entry = _sent.get(chat_id)
    if entry is None:
        entry = (deque(), set())
        _sent[chat_id] = entry
        # Ограничиваем число отслеживаемых чатов (выкидываем самый старый).
        while len(_sent) > _MAX_CHATS:
            _sent.popitem(last=False)
    _sent.move_to_end(chat_id)

    dq, ids = entry
    if message_id in ids:
        return
    dq.append(message_id)
    ids.add(message_id)
    while len(dq) > _MAX_PER_CHAT:
        evicted = dq.popleft()
        ids.discard(evicted)


def is_bot_message(chat_id: Optional[int], message_id: Optional[int]) -> bool:
    """True, если message_id — это известное исходящее сообщение бота в этом чате."""
    if chat_id is None or message_id is None:
        return False
    entry = _sent.get(chat_id)
    return bool(entry and message_id in entry[1])
