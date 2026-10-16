"""Empty, bounded, per-session preview. Never connects to real user data."""
from datetime import datetime, timezone
import uuid

from .types import ConversationError, identifier, page_size, profile


class DemoRepository:
    def __init__(self):
        self.rows = {}

    async def ensure_user(self, verified_profile):
        actor = profile(verified_profile)
        old = next((r for r in self.rows.values() if r['owner_id'] == actor['id']), None)
        if old:
            return old['id']
        if len(self.rows) >= 1000:
            raise ConversationError('preview_limit', 503)
        cid, now = uuid.uuid4(), datetime.now(timezone.utc)
        self.rows[cid] = dict(id=cid, owner_id=actor['id'], kind='arti_private', title='Арти', role='owner',
                              created_at=now, updated_at=now, message_count=0, unread_count=0, last_message=None)
        return cid

    async def list(self, actor_id, limit=50):
        page_size(limit)
        return [{k: v for k, v in r.items() if k != 'owner_id'} for r in self.rows.values() if r['owner_id'] == actor_id][:limit]

    async def get(self, actor_id, conversation_id):
        row = self.rows.get(identifier(conversation_id))
        if not row or row['owner_id'] != actor_id:
            raise ConversationError('not_found', 404)
        return {k: v for k, v in row.items() if k != 'owner_id'}

    async def messages(self, actor_id, conversation_id, *, before=None, limit=50):
        page_size(limit)
        if before is not None and (type(before) is not int or not 0 < before < 2**63):
            raise ConversationError('invalid_cursor')
        await self.get(actor_id, conversation_id)
        return dict(items=[], next_before=None)
