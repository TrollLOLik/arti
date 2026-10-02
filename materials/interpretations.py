"""Participant reviews and compact gists, separately from exact source facts."""
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
import math
from materials.derivatives import DerivativeRepository
from materials.types import MaterialError


@dataclass(frozen=True)
class MaterialReview:
    gist: str
    status: str
    reason: str
    questions: tuple[str,...]=()
    stability_days: float=14
    def __post_init__(self):
        if not self.gist or len(self.gist)>2000 or self.status not in ('preferred','rejected','pending','proposal') or len(self.reason)>2000 or len(self.questions)>8 or any(len(q)>400 for q in self.questions) or not math.isfinite(self.stability_days) or not 1<=self.stability_days<=365: raise MaterialError('invalid_material_review')


class InterpretationRepository:
    def __init__(self,materials): self.derivatives=DerivativeRepository(materials); self.pool=materials.pool
    async def record(self,actor,review,refs,*,inputs=(),at=None):
        if actor.user_id is None: raise MaterialError('review_author_required')
        at=at or datetime.now(timezone.utc)
        if at.tzinfo is None: raise MaterialError('review_time_required')
        return await self.derivatives.save(actor,'material_review',dict(review=asdict(review),reviewer=actor.user_id,recorded_at=at.isoformat(),authorship='explicit_participant_review_not_collective_decision'),refs,inputs=inputs)
    async def remove(self,actor,id):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.derivatives.materials._locks(conn,actor)
            value=await self.derivatives._payload(conn,id,actor,'material_review')
            if value['body']['reviewer']!=actor.user_id: raise MaterialError('review_edit_denied')
            await conn.execute('UPDATE material_derivatives SET payload=NULL,invalidated_at=NOW() WHERE id=$1',id)
            await self.derivatives._invalidate(conn,id)
    async def recall(self,actor,asset_ids,*,at=None,limit=4):
        if not asset_ids or len(asset_ids)>200 or not 1<=limit<=12: return []
        at=at or datetime.now(timezone.utc)
        if at.tzinfo is None: raise MaterialError('review_time_required')
        async with self.pool.acquire() as conn:
            rows=await conn.fetch('''SELECT DISTINCT d.id,d.payload#>>'{body,recorded_at}' AS recorded_at FROM material_derivatives d JOIN material_dependencies p ON p.derivative_id=d.id
                WHERE d.realm=$1 AND d.kind='material_review' AND d.payload IS NOT NULL AND d.invalidated_at IS NULL AND p.asset_id=ANY($2)
                ORDER BY recorded_at DESC,d.id LIMIT 32''',actor.realm,list(asset_ids))
        result=[]
        for row in rows:
            try: body=await self.derivatives.load(row['id'],actor,'material_review')
            except MaterialError: continue
            review=MaterialReview(**{**body['review'],'questions':tuple(body['review'].get('questions',()))})
            age=max(0,(at-datetime.fromisoformat(body['recorded_at'])).total_seconds()/86400)
            availability=math.exp(-age/review.stability_days)
            if availability<.05: continue
            result.append(dict(id=row['id'],reviewer=body['reviewer'],gist=review.gist,status=review.status,reason=review.reason,open_questions=review.questions,
                availability=round(availability,6),meaning='participant interpretation; exact facts must be reloaded from current evidence'))
        result.sort(key=lambda r:(-r['availability'],r['id']))
        return result[:limit]
