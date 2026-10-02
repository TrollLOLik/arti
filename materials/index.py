"""Original block index. Authorization is applied in SQL before ranking."""
import json,re
from dataclasses import asdict,dataclass
from hashlib import sha256
from materials.types import EvidenceRef,Locator,MaterialError,canonical


@dataclass(frozen=True)
class SearchHit:
    source: EvidenceRef
    text: str
    kind: str
    role: str
    quality: str
    score: float
    filename: str
    source_id: str
    owner_id: int | None
    observation_id: str | None=None
    segment_id: str | None=None


class MaterialIndex:
    def __init__(self,materials): self.materials=materials; self.pool=materials.pool
    async def save(self,actor,eid,bundle):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            row=await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE',bundle.asset_id)
            self.materials.check(row,actor); await self.materials._source_allowed(conn,actor,row['source_id'],owner_id=row['owner_id'])
            if row['current_version']!=bundle.asset_version: raise MaterialError('stale_index_source')
            digest=await conn.fetchval('SELECT sha256 FROM material_extractions WHERE id=$1 AND asset_id=$2 AND asset_version=$3 AND payload IS NOT NULL',eid,bundle.asset_id,bundle.asset_version)
            if digest!=sha256(canonical(bundle.to_dict()).encode()).hexdigest(): raise MaterialError('index_extraction_integrity_failure')
            records=[]
            for b in bundle.blocks:
                role=b.metadata.get('role',b.kind)
                if not b.text.strip() or role in ('timed_word','timeline_chunk','low_energy_interval'): continue
                text=b.text[:20000]
                records.append((actor.realm,bundle.asset_id,bundle.asset_version,eid,b.block_id,canonical(asdict(b.locator)),b.kind,role,b.quality,text))
            await conn.executemany('''INSERT INTO material_block_index(realm,asset_id,asset_version,extraction_id,block_id,locator,kind,role,quality,text)
                VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10) ON CONFLICT DO NOTHING''',records)
    async def backfill(self,actor,limit=8):
        from materials.types import ExtractionBundle
        if not 1<=limit<=20: raise MaterialError('index_backfill_budget')
        async with self.pool.acquire() as conn:
            rows=await conn.fetch('''SELECT e.id,e.payload,e.sha256,a.id AS aid,a.source_id,a.owner_id FROM material_extractions e
                JOIN material_assets a ON a.id=e.asset_id WHERE a.realm=$1 AND a.scope_key=$2 AND a.current_version=e.asset_version
                AND a.erased_at IS NULL AND a.expires_at>NOW() AND e.payload IS NOT NULL
                AND jsonb_path_exists(e.payload,'$.blocks[*] ? (@.text != "")')
                AND NOT EXISTS(SELECT 1 FROM material_block_index i WHERE i.extraction_id=e.id)
                AND NOT EXISTS(SELECT 1 FROM material_source_tombstones t WHERE t.scope_key=a.identity_key AND t.source_id=a.source_id AND t.owner_id IS NOT DISTINCT FROM a.owner_id)
                ORDER BY e.created_at DESC,e.id LIMIT $3''',actor.realm,actor.scope.key,limit)
        total=0
        for row in rows:
            try:
                await self.materials.read(row['aid'],actor)
                value=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
                if sha256(canonical(value).encode()).hexdigest()!=row['sha256']: raise MaterialError('extraction_integrity_failure')
                await self.save(actor,row['id'],ExtractionBundle.from_dict(value)); total+=1
                async with self.pool.acquire() as conn:
                    head=await conn.fetchval("SELECT observation_id FROM material_observation_heads WHERE realm=$1 AND asset_id=$2 AND kind='transcript'",actor.realm,row['aid'])
                if head: await self.transcript(actor,head)
            except MaterialError: continue
        return total
    async def transcript(self,actor,observation_id):
        from materials.derivatives import DerivativeRepository
        from materials.timeline import Timeline
        from materials.observations import ObservationRepository
        observations=ObservationRepository(self.materials)
        body=await DerivativeRepository(self.materials).load(observation_id,actor,'transcript')
        async with self.pool.acquire() as conn:
            payload=await observations._payload(conn,observation_id,actor,'transcript')
        source=payload['sources'][0]; ref=EvidenceRef(**{**source,'locator':Locator.from_dict(source['locator'])})
        bundle=await self.materials.evidence_bundle(ref,actor); timeline=Timeline.from_dict(body['timeline'])
        blocks={b.metadata.get('segment_id'):b for b in bundle.blocks if b.metadata.get('role')=='speaker_turn'}
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            await observations._sources(conn,actor,[ref])
            head=await conn.fetchval("SELECT observation_id FROM material_observation_heads WHERE realm=$1 AND asset_id=$2 AND kind='transcript'",actor.realm,ref.asset_id)
            if head!=observation_id: raise MaterialError('stale_transcript_index')
            await observations._payload(conn,observation_id,actor,'transcript')
            records=[]
            for s in timeline.segments:
                b=blocks.get(s.id)
                if b and s.text.strip(): records.append((actor.realm,ref.asset_id,ref.asset_version,ref.extraction_id,b.block_id,canonical(asdict(b.locator)),observation_id,s.id,'audio','transcript_current','verified' if s.status=='confirmed' else 'uncertain',s.text))
            await conn.executemany('''INSERT INTO material_block_index(realm,asset_id,asset_version,extraction_id,block_id,locator,observation_id,observation_key,segment_id,kind,role,quality,text)
                VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$7,$8,$9,$10,$11,$12) ON CONFLICT DO NOTHING''',records)
    async def search(self,actor,query,*,limit=8,kinds=(),asset_ids=None):
        words=list(dict.fromkeys(re.findall(r'[^\W_]{2,}',str(query).casefold(),re.UNICODE)))[:16]
        if not words or len(str(query))>2000 or not 1<=limit<=20: return []
        if asset_ids is not None and len(asset_ids)>200: raise MaterialError('search_resource_budget')
        await self.backfill(actor)
        # Explicit OR terms enable retrieval within an ordinary question, without
        # a free-form tsquery supplied by the user. Low score remains uncertain.
        tsquery=' | '.join(words)
        async with self.pool.acquire() as conn:
            rows=await conn.fetch('''SELECT i.*,a.filename,a.source_id,a.owner_id,ts_rank_cd(i.terms,to_tsquery('simple',$3)) AS rank
                FROM material_block_index i JOIN material_assets a ON a.id=i.asset_id
                JOIN material_extractions e ON e.id=i.extraction_id
                WHERE i.realm=$1 AND a.realm=$1 AND a.scope_key=$2 AND a.erased_at IS NULL AND a.expires_at>NOW()
                AND i.asset_version=a.current_version AND e.payload IS NOT NULL
                AND ($4::text[]='{}' OR i.kind=ANY($4)) AND ($5::text[] IS NULL OR a.id=ANY($5))
                AND NOT EXISTS(SELECT 1 FROM material_source_tombstones t WHERE t.scope_key=a.identity_key AND t.owner_id IS NOT DISTINCT FROM a.owner_id AND t.source_id=a.source_id)
                AND NOT EXISTS(SELECT 1 FROM cognitive_events ce JOIN cognitive_contexts c ON c.id=ce.context_id
                    WHERE c.persona_id=$6 AND c.chat_id=$7 AND c.topic_id=$8 AND c.mode=$9 AND c.scene_id=$10
                    AND ce.source_id=a.source_id AND ce.owner_id IS NOT DISTINCT FROM a.owner_id AND ce.suppressed_at IS NOT NULL)
                AND (i.observation_id IS NULL OR EXISTS(SELECT 1 FROM material_observation_heads h JOIN material_derivatives d ON d.id=h.observation_id
                    WHERE h.realm=i.realm AND h.asset_id=i.asset_id AND h.kind='transcript' AND h.observation_id=i.observation_id AND d.payload IS NOT NULL AND d.invalidated_at IS NULL))
                AND (i.role<>'speaker_turn' OR NOT EXISTS(SELECT 1 FROM material_observation_heads h JOIN material_derivatives d ON d.id=h.observation_id
                    WHERE h.realm=i.realm AND h.asset_id=i.asset_id AND h.kind='transcript' AND d.payload IS NOT NULL AND d.invalidated_at IS NULL
                    AND (d.payload#>>'{sources,0,asset_version}')::integer=i.asset_version))
                AND i.terms@@to_tsquery('simple',$3) ORDER BY rank DESC,i.asset_id,i.block_id LIMIT $11''',
                actor.realm,actor.scope.key,tsquery,list(kinds),list(asset_ids) if asset_ids is not None else None,
                actor.scope.persona_id,actor.scope.chat_id,actor.scope.topic_id,actor.scope.mode,actor.scope.scene_id,limit)
        hits=[]
        for row in rows:
            loc=json.loads(row['locator']) if isinstance(row['locator'],str) else row['locator']
            ref=EvidenceRef(row['asset_id'],row['asset_version'],row['extraction_id'],row['block_id'],Locator.from_dict(loc))
            try:
                await self.materials.resolve(ref,actor)
                if row['observation_id']:
                    from materials.derivatives import DerivativeRepository
                    await DerivativeRepository(self.materials).load(row['observation_id'],actor,'transcript')
                hits.append(SearchHit(ref,row['text'],row['kind'],row['role'],row['quality'],float(row['rank']),row['filename'],row['source_id'],row['owner_id'],row['observation_id'],row['segment_id']))
            except MaterialError: continue
        return hits
