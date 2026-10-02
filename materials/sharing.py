"""Explicit source-author grants create scoped copies, never widen original ACL."""
from dataclasses import dataclass
from datetime import datetime,timezone
from materials.types import MaterialError

@dataclass(frozen=True)
class ShareGrant:
    author_id: int
    source_asset_id: str
    source_version: int
    source_generation: int
    source_sha256: str
    destination_scope_key: str
    request_id: str
    expires_at: datetime
    def validate(self,source_actor,destination_actor,row,revision):
        if self.expires_at.tzinfo is None or self.expires_at<=datetime.now(timezone.utc): raise MaterialError('share_grant_expired')
        if source_actor.user_id!=self.author_id or destination_actor.user_id!=self.author_id or row['owner_id']!=self.author_id: raise MaterialError('share_author_required')
        if self.source_asset_id!=row['id'] or self.source_version!=revision['version'] or self.source_version!=row['current_version'] or self.source_generation!=row['generation'] or self.source_sha256!=revision['sha256']: raise MaterialError('share_source_changed')
        if self.destination_scope_key!=destination_actor.scope.key or not self.request_id or len(self.request_id)>256: raise MaterialError('share_audience_changed')


async def share_copy(service,source_actor,destination_actor,grant):
    row,revision,data=await service.read_bytes(grant.source_asset_id,source_actor,grant.source_version)
    service.repository.check(row,source_actor,edit=True); grant.validate(source_actor,destination_actor,row,revision)
    if source_actor.scope.key==destination_actor.scope.key: raise MaterialError('share_requires_audience_change')
    # The source and grant are rechecked under the uploader lock inside register,
    # before a public asset and its disclosure provenance become visible together.
    return await service.ingest(data,row['filename'],destination_actor,
        'share:'+grant.source_asset_id+':'+str(grant.source_version)+':'+grant.request_id,
        'shared:'+grant.source_asset_id+':'+grant.request_id,revision['mime'],share=(source_actor,grant))
