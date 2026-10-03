"""Local semantic retrieval. SQL ownership and source fences precede ranking."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import math
import os
from pathlib import Path

from asyncpg.exceptions import DeadlockDetectedError, LockNotAvailableError, QueryCanceledError

from cognition.serialization import object_value
from cognition.types import MODEL_VERSION

logger = logging.getLogger(__name__)
MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
REPOSITORY = 'Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'
REVISION = 'faf4aa4225822f3bc6376869cb1164e8e3feedd0'
VERSION = 'minilm-multilingual-384:'+REVISION+':source-chunks-v4-token-complete'
_RETRYABLE_DATABASE_ERRORS = (TimeoutError, DeadlockDetectedError, LockNotAvailableError, QueryCanceledError)
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


def token_safe_segments(text, tokenizer, max_tokens):
    """Partition all original characters into actually non-truncated inputs.

    `tokenizer` is a clone of the model tokenizer with truncation/padding off.
    Offsets only propose boundaries: Unicode normalization may expand one
    character to many tokens, so every resulting substring is re-tokenized and
    checked with special tokens included. No decode/re-encode text rewriting.
    """
    text = str(text)
    if max_tokens <= tokenizer.num_special_tokens_to_add(False):
        raise ValueError('semantic_token_budget_invalid')
    if not text:
        return [(0,0,'')]
    result = []
    start = 0
    while start < len(text):
        remainder = text[start:]
        encoded = tokenizer.encode(remainder)
        if len(encoded.ids) <= max_tokens:
            end = len(text)
        else:
            # Exclude the final special-token slot. An offset can still land
            # inside one normalized character, which the next check repairs.
            cut = max((end for _,end in encoded.offsets[:max_tokens-1]),default=1)
            cut = max(1,min(len(remainder),cut))
            if len(tokenizer.encode(remainder[:cut]).ids)>max_tokens:
                low,high,best = 1,cut-1,0
                while low<=high:
                    middle = (low+high)//2
                    if len(tokenizer.encode(remainder[:middle]).ids)<=max_tokens:
                        best,low = middle,middle+1
                    else:
                        high = middle-1
                if not best:
                    raise ValueError('semantic_character_exceeds_token_budget')
                cut = best
            end = start+cut
        piece = text[start:end]
        if len(tokenizer.encode(piece).ids)>max_tokens:
            raise ValueError('semantic_input_would_truncate')
        result.append((start,end,piece))
        start = end
    return result


class LocalEncoder:
    """One bounded CPU worker; inference never downloads models or sends text."""
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else model_directory()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='arti-semantic')
        self.model = None
        self.future = None
        self.unavailable = False
        self.last_status = 'complete'
        self.recommended_batch_size = 32
        self.request_signature = None
        self.tokenizer = None
        self.max_tokens = None
        self._closed = False
        self._encoding = False

    def _release_resources(self):
        self.model = None
        self.tokenizer = None
        self.max_tokens = None
        self.future = None
        self.request_signature = None
        self.intent_references = None
        # ONNX/tokenizer wrappers can retain native allocations through Python
        # cycles. Close is a lifecycle boundary, never part of a query.
        import gc
        gc.collect()

    def _encode(self, texts):
        self._encoding = True
        try:
            if self._closed:
                raise RuntimeError('semantic_encoder_closed')
            return self._encode_impl(texts)
        finally:
            self._encoding = False
            if self._closed:
                self._release_resources()

    def _encode_impl(self, texts):
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
            from tokenizers import Tokenizer
            active = self.model.model.tokenizer
            self.max_tokens = int(active.truncation['max_length'])
            self.tokenizer = Tokenizer.from_str(active.to_str())
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        pieces = []
        groups = []
        for text in texts:
            segments = token_safe_segments(text,self.tokenizer,self.max_tokens)
            groups.append(len(segments))
            pieces.extend(piece for _,_,piece in segments)
        # The library's configured 128-token truncation remains unchanged;
        # every actual input has been proven to fit it, including specials.
        encoded = iter(self.model.embed(pieces,batch_size=4))
        vectors = []
        offset = 0
        for count in groups:
            pooled = [0.]*384
            for piece in pieces[offset:offset+count]:
                weight = max(1,len(self.tokenizer.encode(piece,add_special_tokens=False).ids))
                vector = validated_vector(next(encoded))
                for dimension,value in enumerate(vector):
                    pooled[dimension] += weight*value
            vectors.append(validated_vector(pooled))
            offset += count
        return vectors

    async def encode(self, texts, timeout=.6):
        if self._closed:
            self.last_status = 'unavailable'
            return None
        texts = list(texts)
        signature = hashlib.sha256(json.dumps(texts,ensure_ascii=False).encode()).digest()
        if (self.future is not None and self.future.done() and self.request_signature==signature
                and self.last_status=='timeout' and not self.future.cancelled()):
            try:
                result = self.future.result()
                self.last_status = 'complete'
                return result
            except Exception:
                self.unavailable = True
        if self.unavailable or (self.future is not None and not self.future.done()):
            self.last_status = 'unavailable' if self.unavailable else 'timeout'
            return None
        self.request_signature = signature
        self.future = asyncio.get_running_loop().run_in_executor(self.executor, self._encode, texts)
        self.future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            result = await asyncio.wait_for(asyncio.shield(self.future), timeout)
            self.last_status = 'complete'
            return result
        except asyncio.TimeoutError:
            self.last_status = 'timeout'
            if timeout>=1:
                self.recommended_batch_size = max(1,self.recommended_batch_size//2)
            return None
        except Exception:
            self.unavailable = True
            self.last_status = 'unavailable'
            logger.warning('Semantic index unavailable; run python -m tools.setup_semantic_memory')
            return None

    async def close(self):
        self._closed = True
        future = self.future
        if future is not None and not future.done():
            try:
                await asyncio.wait_for(asyncio.shield(future), 3)
            except (Exception, asyncio.CancelledError):
                pass
        self.executor.shutdown(wait=False, cancel_futures=True)
        # A still-running native call owns these objects. Its finally block
        # releases them, including when close's bounded wait has expired.
        if not self._encoding:
            self._release_resources()


# All semantic reads and both sides of encoding use the same ownership,
# source, dependency, version and suppression fences. JSON string comparison
# avoids throwing on malformed historical event IDs.
_ELIGIBLE = """
    FROM cognitive_artifacts a JOIN cognitive_contexts c ON c.id=a.context_id
    JOIN cognitive_events e ON e.context_id=a.context_id AND e.id::text=a.payload->>'event_id'
        AND e.owner_id IS NOT DISTINCT FROM a.owner_id AND e.source_id=a.payload->>'source_id'
    WHERE a.kind='trace' AND a.model_version=$1 AND c.model_version=$1
      AND a.suppressed_at IS NULL AND a.payload IS NOT NULL
      AND c.authority='active' AND NOT c.rebuilding AND a.projection_epoch=c.suppression_epoch
      AND e.suppressed_at IS NULL AND e.payload IS NOT NULL AND length(e.payload->>'text')>0
      AND e.origin IN ('user','delivered_action')
      AND EXISTS(SELECT 1 FROM cognitive_provenance p WHERE p.context_id=a.context_id
                 AND p.artifact_id=a.id AND p.source_event_id=e.id)
      AND NOT EXISTS(WITH RECURSIVE dependencies(id) AS (
          SELECT p.source_event_id FROM cognitive_provenance p
            WHERE p.context_id=a.context_id AND p.artifact_id=a.id
          UNION
          SELECT d.source_event_id FROM cognitive_event_dependencies d JOIN dependencies prior ON prior.id=d.event_id
            WHERE d.context_id=a.context_id)
          SELECT 1 FROM dependencies d LEFT JOIN cognitive_events dependency
            ON dependency.context_id=a.context_id AND dependency.id=d.id
          WHERE dependency.id IS NULL OR dependency.suppressed_at IS NOT NULL OR dependency.payload IS NULL
            OR dependency.owner_id IS DISTINCT FROM a.owner_id)
"""
_SOURCE_SELECT = """SELECT a.*, c.suppression_epoch,e.id AS source_event_id,e.payload AS source_payload,
    arti_semantic_fingerprint(a.payload,e.payload) AS source_fingerprint,
    arti_private_semantic_chunks(e.payload->>'text') AS total_chunks, length(e.payload->>'text') AS source_length """

# Progress is evidence only when every ordinal in the committed prefix exists.
# A missing/mismatched vector resets that source on the next batch; stale extras
# never count toward completeness or rank in search.
_PROGRESS_SELECT = """SELECT eligible.*,p.generation,p.next_chunk,p.last_attempt_at,
    p.source_fingerprint AS progress_fingerprint,p.projection_epoch AS progress_epoch,
    p.total_chunks AS progress_total,
    (SELECT count(*) FROM cognitive_semantic_vectors v WHERE v.context_id=eligible.context_id
      AND v.artifact_id=eligible.id AND v.embedding_model=$2
      AND v.source_fingerprint=eligible.source_fingerprint AND v.projection_epoch=eligible.suppression_epoch
      AND v.chunk_index<p.next_chunk
      AND v.chunk_start=CASE WHEN eligible.source_length<=420 THEN 0 ELSE v.chunk_index*540 END
      AND v.chunk_end IS NOT DISTINCT FROM CASE WHEN eligible.source_length<=420 THEN NULL::integer
          ELSE least(eligible.source_length,v.chunk_start+640) END) AS valid_chunks
    FROM eligible LEFT JOIN cognitive_semantic_progress p
      ON p.context_id=eligible.context_id AND p.artifact_id=eligible.id AND p.embedding_model=$2"""


def _diagnostics():
    return dict(status='incomplete',semantic_status='incomplete',scope='private',
        total_sources=0,indexed_sources=0,indexed_chunks=0,total_chunks=0,remaining_chunks=0)


class SemanticIndex:
    SEARCH_TIMEOUT = 1.5
    BATCH_CHUNKS = 32

    def __init__(self, pool, encoder=None):
        self.pool = pool
        self.encoder = encoder or LocalEncoder()
        self.task = None
        self.public_index = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name='semantic-index')

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.encoder.close()

    async def _prepare(self, row, allowance):
        """Durably reserve a bounded prefix before CPU work, never source text."""
        async with self.pool.acquire(timeout=1) as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout='1000ms'")
            locked = await conn.fetchval('SELECT id FROM cognitive_contexts WHERE id=$1 FOR UPDATE SKIP LOCKED',row['context_id'])
            if locked is None:
                return None
            current = await conn.fetchrow(_SOURCE_SELECT+_ELIGIBLE+' AND a.id=$2',MODEL_VERSION,row['id'])
            if current is None:
                return None
            current = dict(current)
            progress = await conn.fetchrow("""SELECT p.*,(SELECT count(*) FROM cognitive_semantic_vectors v
                WHERE v.context_id=p.context_id AND v.artifact_id=p.artifact_id AND v.embedding_model=p.embedding_model
                  AND v.source_fingerprint=p.source_fingerprint AND v.projection_epoch=p.projection_epoch
                  AND v.chunk_index<p.next_chunk
                  AND v.chunk_start=CASE WHEN $4::integer<=420 THEN 0 ELSE v.chunk_index*540 END
                  AND v.chunk_end IS NOT DISTINCT FROM CASE WHEN $4::integer<=420 THEN NULL::integer
                      ELSE least($4::integer,v.chunk_start+640) END) AS valid_chunks
                FROM cognitive_semantic_progress p WHERE p.context_id=$1 AND p.artifact_id=$2 AND p.embedding_model=$3
                FOR UPDATE""",current['context_id'],current['id'],VERSION,current['source_length'])
            valid = (progress is not None and progress['source_fingerprint']==current['source_fingerprint']
                and progress['projection_epoch']==current['suppression_epoch']
                and progress['total_chunks']==current['total_chunks'] and progress['valid_chunks']==progress['next_chunk'])
            if not valid:
                await conn.execute('DELETE FROM cognitive_semantic_progress WHERE context_id=$1 AND artifact_id=$2 AND embedding_model=$3',current['context_id'],current['id'],VERSION)
                progress = await conn.fetchrow("""INSERT INTO cognitive_semantic_progress
                    (context_id,artifact_id,embedding_model,source_event_id,source_fingerprint,projection_epoch,total_chunks)
                    VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *""",current['context_id'],current['id'],VERSION,
                    current['source_event_id'],current['source_fingerprint'],current['suppression_epoch'],current['total_chunks'])
            if progress['next_chunk']>=current['total_chunks']:
                return None
            await conn.execute('UPDATE cognitive_semantic_progress SET last_attempt_at=clock_timestamp() WHERE generation=$1',progress['generation'])
            current['generation'] = progress['generation']
            current['next_chunk'] = progress['next_chunk']
        from cognition.source_chunks import source_chunks
        source = object_value(current['source_payload'])['text']
        chunks = (source_chunks(source,start_index=current['next_chunk'],limit=allowance) if len(source)>420 else
            [(0,None,source)])
        return current,chunks

    async def _commit(self, row, chunks, vectors):
        async with self.pool.acquire(timeout=1) as conn,conn.transaction():
            await conn.execute("SET LOCAL statement_timeout='1000ms'")
            locked = await conn.fetchval('SELECT id FROM cognitive_contexts WHERE id=$1 FOR UPDATE SKIP LOCKED',row['context_id'])
            if locked is None:
                return False
            # Do not fence on artifact revision: rehearsal may have changed it
            # while encoding identical semantic content. A generation survives
            # only when source/dependencies/epoch/content remain valid.
            current = await conn.fetchrow(_SOURCE_SELECT+_ELIGIBLE+' AND a.id=$2 FOR SHARE OF a,e',MODEL_VERSION,row['id'])
            if (current is None or current['source_fingerprint']!=row['source_fingerprint']
                    or current['suppression_epoch']!=row['suppression_epoch']):
                return False
            progress = await conn.fetchrow("""SELECT * FROM cognitive_semantic_progress WHERE context_id=$1
                AND artifact_id=$2 AND embedding_model=$3 FOR UPDATE""",row['context_id'],row['id'],VERSION)
            if (progress is None or progress['generation']!=row['generation']
                    or progress['next_chunk']!=row['next_chunk']):
                return False
            for offset,((start,end,_),vector) in enumerate(zip(chunks,vectors)):
                await conn.execute("""INSERT INTO cognitive_semantic_vectors
                    (context_id,artifact_id,embedding_model,vector,chunk_start,chunk_end,chunk_index,source_fingerprint,projection_epoch)
                    VALUES($1,$2,$3,$4::double precision[],$5,$6,$7,$8,$9)
                    ON CONFLICT(context_id,artifact_id,embedding_model,chunk_start) DO UPDATE
                    SET vector=EXCLUDED.vector,chunk_end=EXCLUDED.chunk_end,chunk_index=EXCLUDED.chunk_index,
                        source_fingerprint=EXCLUDED.source_fingerprint,projection_epoch=EXCLUDED.projection_epoch""",
                    row['context_id'],row['id'],VERSION,vector,start,end,row['next_chunk']+offset,
                    row['source_fingerprint'],row['suppression_epoch'])
            await conn.execute("""UPDATE cognitive_semantic_progress SET next_chunk=$2,updated_at=clock_timestamp()
                WHERE generation=$1""",row['generation'],row['next_chunk']+len(chunks))
            return True

    async def backfill(self, limit=4, *, batch_chunks=None):
        """Advance up to `limit` sources, fairly, in one bounded chunk batch.

        Return sources advanced, including partial sources, so existing drain
        loops continue until every permitted window has actually been indexed.
        """
        limit = max(0,min(16,int(limit)))
        budget = max(1,min(self.BATCH_CHUNKS,int(batch_chunks or self.BATCH_CHUNKS),
            getattr(self.encoder,'recommended_batch_size',self.BATCH_CHUNKS)))
        if not limit:
            return 0
        async with self.pool.acquire(timeout=1) as conn, conn.transaction():
            # Lock only contexts of returned candidates. PostgreSQL skips busy
            # rows before satisfying LIMIT, so an old busy context cannot hide
            # every later healthy source. Release before any encoding work.
            rows = await conn.fetch('WITH eligible AS MATERIALIZED ('+_SOURCE_SELECT+_ELIGIBLE+'), progress AS MATERIALIZED ('+_PROGRESS_SELECT+'''
                ) SELECT p.* FROM progress p JOIN cognitive_contexts context_lock ON context_lock.id=p.context_id
                WHERE p.generation IS NULL OR p.progress_fingerprint<>p.source_fingerprint
                  OR p.progress_epoch<>p.suppression_epoch OR p.progress_total<>p.total_chunks
                  OR p.valid_chunks<>p.next_chunk OR p.next_chunk<p.total_chunks
                ORDER BY coalesce(p.last_attempt_at,p.created_at),p.id LIMIT $3
                FOR UPDATE OF context_lock SKIP LOCKED''',MODEL_VERSION,VERSION,min(limit,budget),timeout=1)
        if not rows:
            return 0
        prepared = []
        # Equal shares ensure even a maximum-size source cannot consume the
        # entire batch before other selected sources receive a first window.
        allowance = max(1,budget//len(rows))
        for row in rows:
            try:
                item = await self._prepare(row,allowance)
            except _RETRYABLE_DATABASE_ERRORS:
                # A context/artifact may become busy after selection. Keep
                # unrelated candidates moving and retry this one next pass.
                continue
            if item and item[1]:
                prepared.append(item)
        texts = [text for _,chunks in prepared for _,_,text in chunks]
        if not texts:
            return 0
        vectors = await self.encoder.encode(texts,timeout=5)
        # A short or invalid encoder response is never evidence of coverage.
        if vectors is None or len(vectors)!=len(texts):
            return 0
        try:
            vectors = [validated_vector(v) for v in vectors]
        except (TypeError,ValueError,OverflowError):
            return 0
        offset = count = 0
        for row,chunks in prepared:
            chunk_vectors = vectors[offset:offset+len(chunks)]
            offset += len(chunks)
            try:
                count += bool(await self._commit(row,chunks,chunk_vectors))
            except _RETRYABLE_DATABASE_ERRORS:
                continue
        return count

    async def _coverage(self,conn,cid,owner):
        row = await conn.fetchrow('WITH eligible AS MATERIALIZED ('+_SOURCE_SELECT+_ELIGIBLE+'''
            AND a.context_id=$3 AND a.owner_id IS NOT DISTINCT FROM $4), progress AS MATERIALIZED ('''+_PROGRESS_SELECT+'''),
            coverage AS MATERIALIZED (SELECT *,CASE WHEN progress_fingerprint=source_fingerprint
                AND progress_epoch=suppression_epoch AND progress_total=total_chunks AND valid_chunks=next_chunk
                THEN next_chunk ELSE 0 END AS indexed FROM progress)
            SELECT count(*) AS total_sources,count(*) FILTER(WHERE indexed=total_chunks) AS indexed_sources,
                coalesce(sum(indexed),0)::bigint AS indexed_chunks,coalesce(sum(total_chunks),0)::bigint AS total_chunks
            FROM coverage''',MODEL_VERSION,VERSION,cid,owner,timeout=.4)
        result = dict(row)
        result['remaining_chunks'] = result['total_chunks']-result['indexed_chunks']
        result['status'] = result['semantic_status'] = 'complete' if result['remaining_chunks']==0 else 'incomplete'
        result['scope'] = 'private'
        return result

    async def search(self, cid, owner, query, limit=48):
        from cognition.retrieval import RetrievalResult
        diagnostics = _diagnostics()
        try:
            async with asyncio.timeout(self.SEARCH_TIMEOUT):
                async with self.pool.acquire(timeout=.3) as conn:
                    diagnostics = await self._coverage(conn,cid,owner)
                vectors = await self.encoder.encode([str(query)[:3200]],timeout=.6)
                if vectors is None or len(vectors)!=1:
                    status = getattr(self.encoder,'last_status','unavailable')
                    status = status if status in ('unavailable','timeout') else 'unavailable'
                    diagnostics.update(status=status,semantic_status=status)
                    return RetrievalResult(diagnostics=diagnostics)
                vector = validated_vector(vectors[0])
                async with self.pool.acquire(timeout=.3) as conn, conn.transaction(isolation='repeatable_read',readonly=True):
                    # Recount in the ranking snapshot: encoding may have yielded
                    # while another source arrived or its coverage changed.
                    diagnostics = await self._coverage(conn,cid,owner)
                    # Rank at most three independent windows per source. The
                    # source text and permission checks come from the same SQL
                    # snapshot as ranking; no unrestricted recent-only window.
                    rows = await conn.fetch('WITH eligible AS MATERIALIZED ('+_SOURCE_SELECT+_ELIGIBLE+'''
                        AND a.context_id=$3 AND a.owner_id IS NOT DISTINCT FROM $4), scored AS MATERIALIZED (
                        SELECT a.*,v.chunk_start,v.chunk_end,v.chunk_index,
                            arti_semantic_dot(v.vector,$5::double precision[]) AS semantic_score
                        FROM eligible a JOIN cognitive_semantic_progress p ON p.context_id=a.context_id
                            AND p.artifact_id=a.id AND p.embedding_model=$2
                        JOIN cognitive_semantic_vectors v ON v.context_id=a.context_id AND v.artifact_id=a.id AND v.embedding_model=$2
                        WHERE p.source_fingerprint=a.source_fingerprint AND p.projection_epoch=a.suppression_epoch
                            AND v.source_fingerprint=a.source_fingerprint AND v.projection_epoch=a.suppression_epoch
                            AND v.chunk_index<p.next_chunk
                            AND v.chunk_start=CASE WHEN a.source_length<=420 THEN 0 ELSE v.chunk_index*540 END
                            AND v.chunk_end IS NOT DISTINCT FROM CASE WHEN a.source_length<=420 THEN NULL::integer
                                ELSE least(a.source_length,v.chunk_start+640) END), distinct_windows AS (
                        SELECT *,row_number() OVER(PARTITION BY id,
                            md5(CASE WHEN chunk_end IS NULL THEN payload->>'gist' ELSE
                                substring(source_payload->>'text' FROM chunk_start+1 FOR chunk_end-chunk_start) END)
                            ORDER BY semantic_score DESC,chunk_index) AS duplicate_rank
                        FROM scored WHERE semantic_score>=.42), ranked AS (
                        SELECT *,row_number() OVER(PARTITION BY id ORDER BY semantic_score DESC,chunk_index) AS source_rank
                        FROM distinct_windows WHERE duplicate_rank=1)
                        SELECT * FROM ranked WHERE source_rank<=3 ORDER BY semantic_score DESC,id DESC,chunk_index LIMIT $6''',
                        MODEL_VERSION,VERSION,cid,owner,vector,max(0,min(192,int(limit))),timeout=.65)
                from cognition.source_chunks import chunk_trace
                result = []
                seen = set()
                for row in rows:
                    item = dict(row)
                    payload = object_value(item['payload'])
                    source_event = object_value(item.pop('source_payload'))
                    source = source_event['text']
                    excerpt = source[item['chunk_start']:item['chunk_end']] if item['chunk_end'] is not None else payload['gist']
                    identity = (item['id'],' '.join(excerpt.split()))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if item['chunk_end'] is not None:
                        payload = chunk_trace(payload,source,item['chunk_start'],item['chunk_end'])
                        payload['author_id'] = 'arti' if source_event['evidence']['origin']=='delivered_action' else source_event.get('actor_id')
                        payload['audience'] = source_event.get('audience')
                    item['payload'] = payload
                    result.append(item)
                return RetrievalResult(result,diagnostics=diagnostics)
        except asyncio.CancelledError:
            raise
        except (TimeoutError,asyncio.TimeoutError):
            diagnostics.update(status='timeout',semantic_status='timeout')
        except Exception:
            diagnostics.update(status='unavailable',semantic_status='unavailable')
            logger.warning('Semantic search unavailable; returning bounded lexical fallback without payload logging')
        return RetrievalResult(diagnostics=diagnostics)

    async def run(self):
        from ai.intent_semantics import EXAMPLES
        references=await self.encoder.encode(EXAMPLES,timeout=5)
        if references is not None:
            self.encoder.intent_references=references
        while True:
            count = 0
            for index in (self,self.public_index):
                if index is None:
                    continue
                try:
                    count += await index.backfill()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning('Semantic indexing failed; retrying without payload logging')
            await asyncio.sleep(.1 if count else 3)
