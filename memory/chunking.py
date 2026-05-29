from collections import defaultdict
from typing import Any, Dict, Iterable, List

from memory.normalizer import compact_text

MAX_CHUNK_MESSAGES = 5
MAX_CHUNK_CHARS = 1200
MIN_CHUNK_MESSAGES = 3


def _format_message(message: Dict[str, Any]) -> str:
    created_at = message.get("created_at")
    user_name = message.get("user_name") or "Участник"
    text = compact_text(message.get("message_text") or "", 600)
    if created_at is None:
        return f"{user_name}: {text}"
    if isinstance(created_at, str):
        timestamp = created_at
    else:
        timestamp = created_at.strftime("%Y-%m-%d %H:%M:%S")
    return f"[{timestamp}] {user_name}: {text}"


def _token_estimate(text: str) -> int:
    return max(1, len(text or "") // 4)


def build_compact_chunks(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped = defaultdict(list)
    for message in messages:
        grouped[(message.get("chat_id"), message.get("mode") or "default")].append(message)

    chunks: List[Dict[str, Any]] = []
    for (chat_id, mode), group in grouped.items():
        group.sort(key=lambda item: (item.get("created_at"), item.get("id")))
        current = []
        current_lines = []
        current_chars = 0

        def flush():
            nonlocal current, current_lines, current_chars
            if not current:
                return
            chunk_text = "\n".join(current_lines).strip()
            if chunk_text:
                chunks.append({
                    "chat_id": chat_id,
                    "user_id": current[-1].get("user_id"),
                    "mode": mode,
                    "chunk_text": chunk_text,
                    "message_ids": [int(item["id"]) for item in current],
                    "token_estimate": _token_estimate(chunk_text),
                    "metadata": {"chunker": "compact", "message_count": len(current)},
                })
            current = []
            current_lines = []
            current_chars = 0

        for message in group:
            line = _format_message(message)
            line_len = len(line)
            should_flush = (
                current
                and len(current) >= MIN_CHUNK_MESSAGES
                and (len(current) >= MAX_CHUNK_MESSAGES or current_chars + line_len > MAX_CHUNK_CHARS)
            )
            if should_flush:
                flush()

            current.append(message)
            current_lines.append(line)
            current_chars += line_len

            if len(current) >= MAX_CHUNK_MESSAGES or current_chars >= MAX_CHUNK_CHARS:
                flush()

        flush()

    chunks.sort(key=lambda item: (item["chat_id"], item["mode"], item["message_ids"][0]))
    return chunks
