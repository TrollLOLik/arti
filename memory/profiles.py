"""Retired legacy profile synthesis. Relationships use causal new projections."""

async def refresh_user_profile(*args, **kwargs):
    return {'status': 'retired'}

async def maybe_refresh_user_profile(*args, **kwargs):
    return {'status': 'retired'}

async def get_profile_context(*args, **kwargs):
    return ''
