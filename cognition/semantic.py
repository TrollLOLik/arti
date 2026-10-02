"""Local semantic retrieval. SQL ownership and source fences precede ranking."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import math
import os
from pathlib import Path

from cognition.serialization import object_value
from cognition.types import MODEL_VERSION

logger = logging.getLogger(__name__)
MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
REPOSITORY = 'Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'
REVISION = 'faf4aa4225822f3bc6376869cb1164e8e3feedd0'
VERSION = 'minilm-multilingual-384:'+REVISION+':source-chunks-v2'
FILES = ('model_optimized.onnx','config.json','special_tokens_map.json','tokenizer.json','tokenizer_config.json')


# Engineering ranking signals, not probabilities or participant identity claims.
_QUERY_STOPWORDS = frozenset('the a an and or of to in on for with is was were did do does what when where who how about me my our this that it tell remember please что как где когда кто это тот мне мы мой наш про расскажи напомни пожалуйста было были был'.split())


def query_terms(text):
    from cognition.memory_dynamics import tokens
    # Preserve searchable parts of source identifiers (e.g. PRIVATE_ALPHA).
    return (tokens(text) | tokens(str(text).replace('_',' '))) - _QUERY_STOPWORDS


def trace_names(trace):
    """Only names with source-backed encoded spans, never guessed from capitals."""
    return set().union(*(query_terms(d.get('text','')) for d in trace.get('details',()) if d.get('kind')=='name'))


def retrieval_signals(query, trace, *, requested_names=(), semantic_score=0.):
    """Separate topical/action/name evidence from accessibility and mood.

    Exact name tokens only: no unmeasured alias/inflection resolution. A known
    named distractor cannot substitute for another explicitly queried name.
    """
    words=query_terms(query)
    names=trace_names(trace)
    requested=set(requested_names)
    lexical=len(words & query_terms(trace.get('gist',''))) / max(1,len(words))
    topic=len(words & query_terms(trace.get('topic',''))) / max(1,len(words))
    actions=set().union(*(query_terms(d.get('text','')) for d in trace.get('details',()) if d.get('kind')=='action'))
    action=len(words & actions) / max(1,len(words))
    name_match=len(names & requested) / max(1,len(requested))
    name_conflict=bool(requested and names and not names & requested)
    semantic=max(0.,min(1.,float(semantic_score)))
    # A shared name alone does not answer an event-specific question. Require
    # remaining query content to match lexically, topically, or semantically.
    event_words=words-requested
    event_match=not event_words or bool(event_words & (query_terms(trace.get('gist','')) | query_terms(trace.get('topic','')) | actions)) or semantic>=.42
    direct=bool(lexical or topic or action or semantic>=.42)
    return dict(lexical=lexical,topic=topic,action=action,name=name_match,
                semantic=semantic,eligible=direct and event_match and not name_conflict)


def model_directory():
    return Path(os.getenv('ARTI_SEMANTIC_MODEL_DIR', str(Path(__file__).resolve().parents[1]/'data/models/semantic-minilm')))


def validated_vector(vector):
    if len(vector) != 384 or any(not math.isfinite(float(v)) for v in vector):
        raise ValueError('semantic_vector_invalid')
    norm = math.sqrt(sum(float(v)**2 for v in vector))
    if norm < 1e-8:
        raise ValueError('semantic_vector_empty')
    return [float(v)/norm for v in vector]


class LocalEncoder:
    """One bounded CPU worker; inference never downloads models or sends text."""
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else model_directory()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='arti-semantic')
        self.model = None
        self.future = None
        self.unavailable = False

    def _encode(self, texts):
        if self.model is None:
            manifest = json.loads((self.directory/'manifest.json').read_text(encoding='utf-8'))
            if manifest.get('revision') != REVISION:
                raise ValueError('semantic_model_version')
            for name in FILES:
                with (self.directory/name).open('rb') as source:
                    digest = hashlib.file_digest(source,'sha256').hexdigest()
                if digest != manifest['sha256'][name]:
                    raise ValueError('semantic_model_hash')
            from fastembed import TextEmbedding
            self.model = TextEmbedding(MODEL, specific_model_path=str(self.directory),
                local_files_only=True, threads=2, providers=['CPUExecutionProvider'])
        return [validated_vector(v) for v in self.model.embed(texts, batch_size=4)]

    async def encode(self, texts, timeout=.6):
        if self.unavailable or (self.future is not None and not self.future.done()):
            return None
        self.future = asyncio.get_running_loop().run_in_executor(self.executor, self._encode, list(texts))
        self.future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(self.future), timeout)
        except asyncio.TimeoutError:
            return None
        except Exception:
            self.unavailable = True
            logger.warning('Semantic index unavailable; run python -m tools.setup_semantic_memory')
            return None

    async def close(self):
        if self.future is not None and not self.future.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.future), 3)
            except (Exception, asyncio.CancelledError):
                pass
        self.executor.shutdown(wait=False, cancel_futures=True)


class SemanticIndex:
    def __init__(self, pool, encoder=None):
        self.pool = pool
        self.encoder = encoder or LocalEncoder()
        self.task = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name='semantic-index')

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.encoder.close()

    async def backfill(self, limit=4):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''SELECT a.id,a.context_id,a.revision,a.payload,c.suppression_epoch,e.payload AS source_payload
                FROM cognitive_artifacts a JOIN cognitive_contexts c ON c.id=a.context_id
                JOIN cognitive_events e ON e.context_id=a.context_id AND e.id=(a.payload->>'event_id')::bigint
                    AND e.owner_id IS NOT DISTINCT FROM a.owner_id
                    AND e.source_id=a.payload->>'source_id'
                    AND e.suppressed_at IS NULL AND e.payload IS NOT NULL
                WHERE a.kind='trace' AND a.model_version=$1 AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
                AND c.authority='active' AND NOT c.rebuilding AND a.projection_epoch=c.suppression_epoch
                AND NOT EXISTS(SELECT 1 FROM cognitive_semantic_vectors v WHERE v.context_id=a.context_id AND v.artifact_id=a.id AND v.embedding_model=$2)
                AND NOT EXISTS(SELECT 1 FROM cognitive_provenance p JOIN cognitive_events e ON e.id=p.source_event_id WHERE p.artifact_id=a.id AND e.suppressed_at IS NOT NULL)
                ORDER BY a.id LIMIT $3''', MODEL_VERSION, VERSION, min(16, limit))
        if not rows:
            return 0
        from cognition.source_chunks import source_chunks
        prepared = []
        total_chunks = 0
        for row in rows:
            p = object_value(row['payload'])
            source = object_value(row['source_payload'])['text']
            # Keep existing short-source ranking. Long utterances get independent
            # windows so a meaningful tail is not averaged into filler or lost.
            chunks = source_chunks(source) if len(source)>420 else [(0,None,p['gist'][:2400]+'\n'+p.get('interpretation','')[:800])]
            if prepared and total_chunks+len(chunks)>32:
                break
            prepared.append((row,chunks))
            total_chunks += len(chunks)
        texts = [text for _,chunks in prepared for _,_,text in chunks]
        vectors = await self.encoder.encode(texts, timeout=5)
        if vectors is None:
            return 0
        offset = 0
        count = 0
        for row,chunks in prepared:
            chunk_vectors = vectors[offset:offset+len(chunks)]; offset += len(chunks)
            async with self.pool.acquire() as conn, conn.transaction():
                # Same lock order as forget: a delayed encoding cannot resurrect
                # source text, and all windows for this revision commit together.
                await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE', row['context_id'])
                inserted = 0
                for (start,end,_), vector in zip(chunks,chunk_vectors):
                    result = await conn.execute('''INSERT INTO cognitive_semantic_vectors(context_id,artifact_id,embedding_model,vector,chunk_start,chunk_end)
                        SELECT a.context_id,a.id,$3,$4::double precision[],$7,$8 FROM cognitive_artifacts a JOIN cognitive_contexts c ON c.id=a.context_id
                        WHERE a.id=$1 AND a.revision=$2 AND a.model_version=$5 AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
                        AND NOT c.rebuilding AND c.authority='active' AND c.suppression_epoch=$6 AND a.projection_epoch=c.suppression_epoch
                        AND NOT EXISTS(SELECT 1 FROM cognitive_provenance p JOIN cognitive_events e ON e.id=p.source_event_id WHERE p.artifact_id=a.id AND (e.suppressed_at IS NOT NULL OR e.payload IS NULL))
                        ON CONFLICT DO NOTHING''',row['id'],row['revision'],VERSION,validated_vector(vector),MODEL_VERSION,row['suppression_epoch'],start,end)
                    inserted += int(result.rsplit(' ', 1)[-1])
                count += bool(inserted)
        return count

    async def search(self, cid, owner, query, limit=48):
        vectors = await self.encoder.encode([str(query)[:3200]], timeout=.6)
        if vectors is None:
            return []
        vector = validated_vector(vectors[0])
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''SELECT * FROM (
                SELECT DISTINCT ON(a.id) a.*,v.chunk_start,v.chunk_end,e.payload AS source_payload,
                    arti_semantic_dot(v.vector,$5::double precision[]) AS semantic_score
                FROM cognitive_semantic_vectors v JOIN cognitive_artifacts a ON a.context_id=v.context_id AND a.id=v.artifact_id
                JOIN cognitive_contexts c ON c.id=a.context_id
                JOIN cognitive_events e ON e.context_id=a.context_id AND e.id=(a.payload->>'event_id')::bigint
                    AND e.owner_id IS NOT DISTINCT FROM a.owner_id AND e.source_id=a.payload->>'source_id'
                WHERE a.context_id=$1 AND a.owner_id IS NOT DISTINCT FROM $2 AND a.kind='trace'
                AND a.model_version=$3 AND v.embedding_model=$4 AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
                AND e.suppressed_at IS NULL AND e.payload IS NOT NULL
                AND NOT c.rebuilding AND a.projection_epoch=c.suppression_epoch
                AND NOT EXISTS(SELECT 1 FROM cognitive_provenance p JOIN cognitive_events e ON e.id=p.source_event_id WHERE p.artifact_id=a.id AND (e.suppressed_at IS NOT NULL OR e.payload IS NULL))
                ORDER BY a.id,semantic_score DESC,v.chunk_start) ranked
                ORDER BY semantic_score DESC,id DESC LIMIT $6''',cid,owner,MODEL_VERSION,VERSION,vector,min(64,limit))
        from cognition.source_chunks import chunk_trace
        result = []
        for row in rows:
            if row['semantic_score']<.42:
                continue
            item = dict(row)
            p = object_value(item['payload'])
            source_event = object_value(item.pop('source_payload'))
            source = source_event['text']
            if item['chunk_end'] is not None:
                p = chunk_trace(p,source,item['chunk_start'],item['chunk_end'])
                p['author_id'] = 'arti' if source_event['evidence']['origin']=='delivered_action' else source_event.get('actor_id')
                p['audience'] = source_event.get('audience')
            item['payload'] = p
            result.append(item)
        return result

    async def run(self):
        from ai.intent_semantics import EXAMPLES
        references=await self.encoder.encode(EXAMPLES,timeout=5)
        if references is not None:
            self.encoder.intent_references=references
        while True:
            try:
                count = await self.backfill()
            except asyncio.CancelledError:
                raise
            except Exception:
                count = 0
                logger.warning('Semantic indexing failed; retrying without payload logging')
            await asyncio.sleep(.1 if count else 3)
