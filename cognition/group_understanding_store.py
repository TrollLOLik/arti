"""Durable, bounded conversation hypotheses over the observed public ledger.

Only raw, freshly permitted observations enter the analyzer. Complete cumulative
input/selection lineage is retained independently of the model's citations. Any
revocation erases the whole hypothesis; a replacement replays surviving raw
history, never an edited old summary. Provider calls hold no database locks.
"""
import asyncio
import json
import uuid
from datetime import timedelta

from asyncpg import QueryCanceledError

from cognition.group_understanding import VERSION, normalize_messages
from cognition.initiative_policy import provider_slot
from cognition.public_memory import PublicMemoryRepository, _VISIBLE
from cognition.serialization import dump, object_value
from cognition.types import ContextKey, MODEL_VERSION, utc

NEW_EVENT_LIMIT = 24
INPUT_SOURCE_LIMIT = 72
INPUT_BYTE_LIMIT = 48000
TEXT_CHAR_LIMIT = 2400
LINEAGE_LIMIT = 4096
ANALYZE_TIMEOUT_SECONDS = 8.
REFRESH_TIMEOUT_SECONDS = 12.
LEASE_SECONDS = 30
_HASH = "md5(jsonb_build_array(p.payload,p.observation_payload,p.source_id,p.owner_id,p.event_key,p.origin,p.message_id,p.observed_at,p.occurred_at)::text)"


def _context(row):
    return ContextKey(*(row[k] for k in ('persona_id', 'chat_id', 'mode', 'scene_id', 'topic_id')))


def _messages(rows, context):
    """Deterministic excerpts; full raw hashes remain the revocation boundary."""
    result = []
    for row in sorted(rows, key=lambda r: r['id']):
        source = PublicMemoryRepository._source(row, context)
        if source is None:
            continue
        event, observation = source
        text = event.text[:TEXT_CHAR_LIMIT]
        message = dict(source_id=row['source_id'], message_id=row['message_id'],
            owner_id=row['owner_id'], sender_kind=observation['sender_kind'],
            directed=observation.get('directed'), reply_to_id=observation.get('reply_to_id'),
            at=event.observed_at.isoformat(), text=text, text_truncated=len(text) != len(event.text))
        try:
            normalize_messages([message])
        except (ValueError, TypeError, KeyError):
            continue
        result.append(message)
    # Keep every chosen evidence anchor, but explicitly shorten source excerpts
    # to fit one global UTF-8 budget. Never slice a multibyte character.
    while len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > INPUT_BYTE_LIMIT:
        longest = max(result, key=lambda m: len(m['text'].encode('utf-8')))
        if not longest['text']:
            raise ValueError('Observation metadata exceeds input budget')
        longest['text'] = longest['text'][:max(0, len(longest['text']) * 3 // 4)]
        longest['text_truncated'] = True
    return result


class ConversationUnderstanding:
    def __init__(self, runtime, analyzer=None):
        self.runtime, self.pool = runtime, runtime.pool
        self.repository = PublicMemoryRepository(self.pool)
        if analyzer is None:
            from ai.group_understanding import SelectedModelGroupUnderstanding
            analyzer = SelectedModelGroupUnderstanding()
        self.analyzer = analyzer

    async def close(self):
        close = getattr(self.analyzer, 'close', None)
        if close:
            await close()

    async def _scope(self, conn, cid, context, at, requester=None, *, locked=True):
        if locked:
            return await self.repository._scope(conn, cid, context, utc(at), requester, None)
        return await self.repository._scope(conn, cid, context, utc(at), requester, None, lock=False)

    async def _erase(self, conn, cid):
        await conn.execute('SELECT arti_erase_group_understanding($1)', cid)

    async def _valid_state(self, conn, cid, context, scope, *, erase=True):
        """Validate all consulted lineage, including uncited and old inputs."""
        ctx, args = scope
        state = await conn.fetchrow('SELECT * FROM group_understanding_state WHERE context_id=$1', cid)
        if state is None:
            return None
        _, policy_revision = await self.repository.policies.get(context.chat_id, context.topic_id, conn)
        if (state['schema_version'] != VERSION or state['projection_epoch'] != ctx['suppression_epoch']
                or state['policy_revision'] != policy_revision):
            if erase:
                await self._erase(conn, cid)
            return None
        invalid = await conn.fetchval(_VISIBLE + f'''
            SELECT EXISTS(SELECT 1 FROM group_understanding_dependencies d
              LEFT JOIN permitted p ON p.id=d.event_id
              WHERE d.context_id=$1 AND (p.id IS NULL OR d.source_hash IS DISTINCT FROM {_HASH}
                OR d.source_id IS DISTINCT FROM p.source_id)) OR EXISTS(
                SELECT 1 FROM group_understanding_skips s LEFT JOIN permitted p ON p.id=s.event_id
                WHERE s.context_id=$1 AND (p.id IS NULL OR s.source_hash IS DISTINCT FROM {_HASH}))''', *args)
        shape_valid = await conn.fetchval('''SELECT
            cardinality($2::bigint[])<=$3
            AND cardinality($2::bigint[])=(SELECT count(DISTINCT id) FROM unnest($2::bigint[]) AS ids(id))
            AND (SELECT count(*) FROM group_understanding_dependencies WHERE context_id=$1)<=$4
            AND NOT EXISTS(SELECT 1 FROM unnest($2::bigint[]) AS ids(id) WHERE NOT EXISTS(
                SELECT 1 FROM group_understanding_dependencies d WHERE d.context_id=$1 AND d.event_id=ids.id))''',
            cid, state['input_event_ids'], INPUT_SOURCE_LIMIT, LINEAGE_LIMIT)
        if invalid or not shape_valid or state['cursor_event_id'] < ctx['history_after_event_id']:
            if erase:
                await self._erase(conn, cid)
            return None
        return state

    async def _input_rows(self, conn, args, ids):
        return await conn.fetch(_VISIBLE + f'''
            SELECT p.*, {_HASH} AS source_hash FROM permitted p
            WHERE p.id=ANY($8::bigint[]) ORDER BY p.id''', *args, list(ids))

    async def _lineage_rows(self, conn, args, ids):
        # Include every transitive ancestor in downstream provenance. Merely
        # checking ancestor readability is insufficient: erasure propagation
        # can intentionally stop at a foreign human author in other pipelines.
        return await conn.fetch(_VISIBLE + f'''SELECT p.id,p.source_id,{_HASH} AS source_hash
            FROM permitted p WHERE p.id IN (
                SELECT event_id FROM dependencies WHERE root_id=ANY($8::bigint[]))
            ORDER BY p.id LIMIT $9''', *args, list(ids), LINEAGE_LIMIT + 1)

    async def _payload(self, conn, state, context, args):
        if state['payload'] is None:
            return None
        from cognition.group_understanding import revalidate_understanding
        rows = await self._input_rows(conn, args, state['input_event_ids'])
        if len(rows) != len(state['input_event_ids']):
            return None
        try:
            return revalidate_understanding(object_value(state['payload']), _messages(rows, context))
        except (ValueError, TypeError, KeyError):
            return None

    async def read_locked(self, conn, cid, context, at, requester=None):
        """No inference. Caller uses a writable transaction and context/chat lock.

        source_event_ids is complete lineage, not merely output citations. A
        consumer using the payload in a generated answer must propagate it all.
        """
        return await self._read(conn, cid, context, at, requester, locked=True)

    async def read_snapshot(self, conn, cid, context, at, requester=None):
        """Repeatable-read frame projection; final use requires locked validation."""
        return await self._read(conn, cid, context, at, requester, locked=False)

    async def _read(self, conn, cid, context, at, requester, *, locked):
        empty = dict(payload=None, status='unavailable', current=False, as_of_event_id=None,
                     source_event_ids=[], source_ids=[], bounded=True, history_complete=False)
        scope = await self._scope(conn, cid, context, at, requester, locked=locked)
        if scope is None:
            # A requester opt-out is a read restriction, not an instruction to
            # erase another participant's otherwise permitted public cache.
            if locked and requester is None and isinstance(context, ContextKey):
                identity = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1', cid)
                if identity is not None and _context(identity) == context:
                    await self._erase(conn, cid)
            return empty
        state = await self._valid_state(conn, cid, context, scope, erase=locked)
        if state is None:
            return empty
        skipped = await conn.fetchval('SELECT count(*) FROM group_understanding_skips WHERE context_id=$1', cid)
        if state['payload'] is None:
            return {**empty, 'status': 'incomplete' if skipped else 'unavailable',
                    'as_of_event_id': state['cursor_event_id'], 'skipped_source_count': skipped,
                    'coverage_gaps': bool(skipped), 'lineage_reset': state['lineage_truncated']}
        payload = await self._payload(conn, state, context, scope[1])
        if payload is None:
            if locked:
                await self._erase(conn, cid)
            return empty
        pending = await conn.fetchval(_VISIBLE + 'SELECT EXISTS(SELECT 1 FROM permitted WHERE id>$8)',
                                      *scope[1], state['cursor_event_id'])
        revision = await conn.fetchval('SELECT revision FROM group_topic_runtime WHERE context_id=$1', cid) or 0
        dependencies = await conn.fetch('''SELECT event_id,source_id FROM group_understanding_dependencies
            WHERE context_id=$1 ORDER BY event_id''', cid)
        return dict(payload=payload, status='ready', current=not pending and not skipped and revision == state['group_revision'],
            skipped_source_count=skipped, coverage_gaps=bool(skipped),
            as_of_event_id=state['cursor_event_id'], group_revision=state['group_revision'], generation=state['generation'],
            schema_version=state['schema_version'], projection_epoch=state['projection_epoch'], committed_at=state['committed_at'].isoformat(),
            source_event_ids=[r['event_id'] for r in dependencies], source_ids=[r['source_id'] for r in dependencies],
            input_event_ids=list(state['input_event_ids']), bounded=True, history_complete=False,
            lineage_truncated=state['lineage_truncated'], lineage_reset=state['lineage_truncated'],
            coverage_start_event_id=state['coverage_start_event_id'])

    async def _revision(self, conn, cid):
        row = await conn.fetchrow('''SELECT coalesce(r.revision,0) AS revision,coalesce(r.closed,FALSE) AS closed,
            (SELECT count(*) FROM group_observations WHERE context_id=$1) AS observations
            FROM cognitive_contexts c LEFT JOIN group_topic_runtime r ON r.context_id=c.id WHERE c.id=$1''', cid)
        return dict(row)

    async def _skip(self, conn, cid, row, reason):
        await conn.execute('''INSERT INTO group_understanding_skips(context_id,event_id,source_hash,reason)
            VALUES($1,$2,$3,$4) ON CONFLICT(context_id,event_id) DO UPDATE
            SET source_hash=EXCLUDED.source_hash,reason=EXCLUDED.reason''',
            cid, row['id'], row['source_hash'], reason)

    async def _prepare(self, cid):
        from cognition.group_understanding import retained_sources
        async with self.pool.acquire(timeout=1) as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '900ms'")
            ctx = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE SKIP LOCKED', cid)
            if ctx is None or ctx['topic_id'] < 0:
                return None
            context = _context(ctx)
            if not await conn.fetchval('SELECT pg_try_advisory_xact_lock($1::bigint)', context.chat_id):
                return None
            now = utc(self.runtime.clock())
            scope = await self._scope(conn, cid, context, now)
            if scope is None:
                await self._erase(conn, cid)
                return None
            ctx, args = scope
            state = await self._valid_state(conn, cid, context, scope)
            if state and state['lease_token'] and state['lease_until'] > now:
                return None
            if state is None:
                _, policy_revision = await self.repository.policies.get(context.chat_id, context.topic_id, conn)
                state = await conn.fetchrow('''INSERT INTO group_understanding_state
                    (context_id,cursor_event_id,coverage_start_event_id,projection_epoch,policy_revision,schema_version)
                    VALUES($1,$2,$2,$3,$4,$5) RETURNING *''', cid, ctx['history_after_event_id'],
                    ctx['suppression_epoch'], policy_revision, VERSION)
            raw = await conn.fetch(_VISIBLE + f'''SELECT p.*, {_HASH} AS source_hash FROM permitted p
                WHERE p.id>$8 ORDER BY p.id LIMIT $9''', *args, state['cursor_event_id'], NEW_EVENT_LIMIT)
            if not raw:
                return None
            rows = [r for r in raw if _messages([r], context)]
            usable = {r['id'] for r in rows}
            for invalid in raw:
                if invalid['id'] not in usable:
                    await self._skip(conn, cid, invalid, 'invalid_wire')
            target = raw[-1]['id']
            if not rows:
                # Invalid wire records are not evidence and cannot pin progress.
                await conn.execute('UPDATE group_understanding_state SET cursor_event_id=$2 WHERE context_id=$1', cid, target)
                return None
            old_payload = await self._payload(conn, state, context, args)
            if state['payload'] is not None and old_payload is None:
                await self._erase(conn, cid)
                return None
            dependencies = await conn.fetch('SELECT event_id FROM group_understanding_dependencies WHERE context_id=$1', cid)
            prior_ids = {r['event_id'] for r in dependencies}
            fresh_rows = list(rows)
            if old_payload:
                anchors = retained_sources(old_payload, INPUT_SOURCE_LIMIT - len(rows))
                anchor_rows = await conn.fetch(_VISIBLE + f'''SELECT p.*, {_HASH} AS source_hash FROM permitted p
                    WHERE p.source_id=ANY($8::text[]) ORDER BY p.id''', *args, anchors)
                rows = list({r['id']: r for r in [*anchor_rows, *rows]}.values())
            lineage = await self._lineage_rows(conn, args, [r['id'] for r in rows])
            reset = len(prior_ids | {r['id'] for r in lineage}) > LINEAGE_LIMIT
            if reset:
                # Select independently of the old generated output; retain no
                # hidden influence from its anchor choices or interpretation.
                rows = fresh_rows
                lineage = await self._lineage_rows(conn, args, [r['id'] for r in rows])
                # A large dependency graph may require a smaller sequential
                # prefix. Quarantine an unrepresentable first root so independent
                # successors can progress, with an explicit coverage gap.
                while len(lineage) > LINEAGE_LIMIT and len(rows) > 1:
                    rows = rows[:max(1, len(rows) // 2)]
                    target = rows[-1]['id']
                    lineage = await self._lineage_rows(conn, args, [r['id'] for r in rows])
                if len(lineage) > LINEAGE_LIMIT:
                    await self._skip(conn, cid, rows[0], 'lineage_limit')
                    await conn.execute('UPDATE group_understanding_state SET cursor_event_id=$2 WHERE context_id=$1', cid, rows[0]['id'])
                    return None
                await conn.execute('DELETE FROM group_understanding_dependencies WHERE context_id=$1', cid)
                await conn.execute('''UPDATE group_understanding_state SET payload=NULL,input_event_ids='{}',
                    lineage_truncated=TRUE,coverage_start_event_id=cursor_event_id WHERE context_id=$1''', cid)
                old_payload = None
            rows.sort(key=lambda r: r['id'])
            messages = _messages(rows, context)
            if len(messages) != len(rows) or len(messages) > INPUT_SOURCE_LIMIT:
                return None
            token = uuid.uuid4().hex
            await conn.executemany('''INSERT INTO group_understanding_dependencies(context_id,event_id,source_id,source_hash)
                VALUES($1,$2,$3,$4) ON CONFLICT(context_id,event_id) DO NOTHING''',
                [(cid, r['id'], r['source_id'], r['source_hash']) for r in lineage])
            await conn.execute('''UPDATE group_understanding_state SET lease_token=$2,lease_until=$3,pending_cursor=$4
                WHERE context_id=$1''', cid, token, now + timedelta(seconds=LEASE_SECONDS), target)
            revision = await self._revision(conn, cid)
            return dict(cid=cid, context=context, token=token, generation=state['generation'],
                cursor=state['cursor_event_id'], target=target, epoch=state['projection_epoch'],
                policy_revision=state['policy_revision'], input_ids=[r['id'] for r in rows], messages=messages,
                revision=revision, hashes={r['id']: r['source_hash'] for r in rows}, previous=old_payload)

    async def _check(self, conn, prepared):
        p = prepared
        now = utc(self.runtime.clock())
        await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', p['context'].chat_id)
        scope = await self._scope(conn, p['cid'], p['context'], now)
        if scope is None:
            await self._erase(conn, p['cid'])
            return None
        state = await self._valid_state(conn, p['cid'], p['context'], scope)
        if (state is None or state['generation'] != p['generation'] or state['lease_token'] != p['token']
                or state['lease_until'] <= now or state['cursor_event_id'] != p['cursor']
                or state['projection_epoch'] != p['epoch'] or state['policy_revision'] != p['policy_revision']):
            return None
        rows = await self._input_rows(conn, scope[1], p['input_ids'])
        if {r['id']: r['source_hash'] for r in rows} != p['hashes'] or _messages(rows, p['context']) != p['messages']:
            await self._erase(conn, p['cid'])
            return None
        revision = await self._revision(conn, p['cid'])
        previous = p['revision']
        # New observations may coexist with this explicitly bounded prefix.
        # Feedback, closure, or other revision changes require a fresh attempt.
        if (revision['closed'] != previous['closed']
                or revision['revision'] - previous['revision'] != revision['observations'] - previous['observations']):
            return None
        return revision

    async def _refresh(self, cid):
        prepared = await self._prepare(cid)
        if prepared is None:
            return 0
        try:
            async with provider_slot(self.runtime, cid, None, 'group_understanding', hourly_limit=6) as admitted:
                if not admitted:
                    return 0
                async with self.pool.acquire(timeout=1) as conn, conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = '900ms'")
                    if await self._check(conn, prepared) is None:
                        return 0
                async with asyncio.timeout(ANALYZE_TIMEOUT_SECONDS):
                    result = await self.analyzer.analyze(prepared['messages'], prepared['context'].chat_id)
                from cognition.group_understanding import reconcile_understanding
                payload = reconcile_understanding(prepared['previous'], result, prepared['messages'])
                async with self.pool.acquire(timeout=1) as conn, conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = '900ms'")
                    revision = await self._check(conn, prepared)
                    if revision is None:
                        return 0
                    # Fence an already prepared assessment using the old graph.
                    current_revision = await conn.fetchval('''INSERT INTO group_topic_runtime(context_id,revision) VALUES($1,1)
                        ON CONFLICT(context_id) DO UPDATE SET revision=group_topic_runtime.revision+1,
                        updated_at=clock_timestamp() RETURNING revision''', cid)
                    await conn.execute('''UPDATE group_understanding_state SET payload=$2::jsonb,
                        generation=nextval(pg_get_serial_sequence('group_understanding_state','generation')),
                        cursor_event_id=$3,pending_cursor=0,input_event_ids=$4,group_revision=$5,committed_at=$6,
                        lease_token=NULL,lease_until=NULL WHERE context_id=$1''', cid, dump(payload), prepared['target'],
                        prepared['input_ids'], current_revision, utc(self.runtime.clock()))
                return 1
        finally:
            async def release():
                async with self.pool.acquire(timeout=.4) as conn:
                    await conn.execute('''UPDATE group_understanding_state SET lease_token=NULL,lease_until=NULL,pending_cursor=0
                        WHERE context_id=$1 AND lease_token=$2''', cid, prepared['token'], timeout=.4)
            from utils.async_cleanup import await_owned
            try:
                await await_owned(release(), timeout=.5)
            except (TimeoutError, OSError, QueryCanceledError):
                pass

    async def refresh(self, cid):
        """One bounded background extraction. Failure never advances its cursor."""
        try:
            async with asyncio.timeout(REFRESH_TIMEOUT_SECONDS):
                return await self._refresh(cid)
        except (TimeoutError, QueryCanceledError, OSError, ValueError, TypeError, KeyError):
            return 0
        except Exception:
            # Optional interpretation must not break observation or arbitration.
            return 0

    async def sweep(self, limit=1):
        """Fair durable rotation, including unavailable/locked/failed contexts."""
        limit = max(0, min(8, int(limit)))
        if not limit:
            return 0
        try:
            async with self.pool.acquire(timeout=1) as conn, conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '900ms'")
                rows = await conn.fetch('''SELECT c.id FROM cognitive_contexts c
                    LEFT JOIN group_understanding_work w ON w.context_id=c.id
                    WHERE c.topic_id>=0 AND c.authority='active' AND NOT c.rebuilding AND c.model_version=$1
                    AND EXISTS(SELECT 1 FROM group_observations o WHERE o.context_id=c.id)
                    ORDER BY coalesce(w.attempted_at,'-infinity'::timestamptz),c.id LIMIT $2
                    FOR NO KEY UPDATE OF c SKIP LOCKED''', MODEL_VERSION, limit)
                for row in rows:
                    await conn.execute('''INSERT INTO group_understanding_work(context_id) VALUES($1)
                        ON CONFLICT(context_id) DO UPDATE SET attempted_at=clock_timestamp()''', row['id'])
            count = 0
            for row in rows:
                count += await self.refresh(row['id'])
            return count
        except (TimeoutError, QueryCanceledError, OSError):
            return 0
