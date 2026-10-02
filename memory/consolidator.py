"""Retired legacy consolidation; retained callers cannot invoke a provider."""

async def consolidate_chat_facts(*args, **kwargs):
    return {'status': 'retired', 'new_fact_count': 0}

async def maybe_consolidate(*args, **kwargs):
    return {'status': 'retired', 'new_fact_count': 0}
