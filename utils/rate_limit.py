"""Простой in-memory sliding-window rate-limiter по пользователю (AUTH-05).

Используется для ограничения дорогих/платных действий (генерация медиа), чтобы
один участник не флудил очередь и не жёг внешние API. Состояние живёт в памяти
процесса и не переживает рестарт — для жёстких продакшен-квот нужен Redis/БД
(см. отчёт, S-18), но даже памяти достаточно, чтобы закрыть основной abuse-вектор.
"""
import time
from typing import Dict, List

# scope -> {user_id: [monotonic-таймстампы попыток в окне]}
_buckets: Dict[str, Dict[int, List[float]]] = {}


def is_rate_limited(scope: str, user_id: int, limit: int, window_sec: float) -> bool:
    """True, если по (scope, user_id) превышен лимит за окно. Иначе фиксирует попытку.

    Вызов с превышением НЕ добавляет новую отметку (чтобы окно «не уезжало» от спама).
    """
    now = time.monotonic()
    bucket = _buckets.setdefault(scope, {})
    timestamps = [t for t in bucket.get(user_id, []) if now - t < window_sec]
    if len(timestamps) >= limit:
        bucket[user_id] = timestamps
        return True
    timestamps.append(now)
    bucket[user_id] = timestamps
    return False
