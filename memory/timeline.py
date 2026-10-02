"""Retired timeline synthesis; new episodes retain source-backed temporal context."""

async def build_timeline_events(*args, **kwargs):
    return {'status': 'retired'}

async def get_timeline_context(*args, **kwargs):
    return ''
