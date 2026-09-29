"""Bounded, escaped memory payloads; framing is never cut along with data."""
from html import escape


def bounded_text(text: str, limit: int, *, tail: bool = False) -> str:
    text = str(text or '')
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:max(0, limit)]
    return ('…' + text[-(limit - 1):]) if tail else (text[:limit - 1] + '…')


def memory_payload(sections: list[tuple[str, str, int]], limit: int = 6000) -> str:
    """Allocate per-source quotas before framing; raw markup cannot close the fence."""
    opening = '<user_memory>\n'
    closing = '\n</user_memory>'
    remaining = limit - len(opening) - len(closing)
    blocks = []
    for title, text, quota in sections:
        if not text or remaining < len(title) + 10:
            continue
        header = escape(title, quote=False) + ':\n'
        data = escape(str(text), quote=False)
        block = header + bounded_text(data, min(quota, remaining - len(header) - 2))
        blocks.append(block)
        remaining -= len(block) + 2
    return opening + '\n\n'.join(blocks) + closing if blocks else ''


def generation_context(dialogue: str, memory: str = '', prompt: str = '',
                       dialogue_limit: int = 12000, memory_limit: int = 7000) -> str:
    """Reserve independent budgets for recent dialogue and complete memory framing."""
    dialogue = str(dialogue or '')
    lines = dialogue.split('\n')
    if lines and lines[-1].strip() == str(prompt).strip():
        dialogue = '\n'.join(lines[:-1])
    parts = []
    if dialogue:
        parts.append('[Недавний диалог]\n' + bounded_text(dialogue, dialogue_limit, tail=True))
    if memory:
        # Do not truncate a fenced object at generation time. Oversized objects are
        # treated as data and re-framed; normal retrieval fits its smaller budget.
        if len(memory) > memory_limit:
            memory = memory_payload([('Память', memory, memory_limit - 80)], memory_limit)
        parts.append(memory)
    return '\n\n'.join(parts)
