"""Explicit operational commands; importing this module never starts the bot."""
import argparse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from dotenv import dotenv_values,load_dotenv
import asyncpg

from cognition.diagnostics import operational_metrics
from cognition.migration import HistoricalMigration
from cognition.repositories import ensure_schema
from cognition.runtime import CognitiveRuntime
from cognition.types import ContextKey

TABLES = ('memory_messages','memory_facts','memory_timelines','memory_user_profiles',
          'memory_wiki_pages','memory_entities','memory_relations','memory_chunks')


def parameters():
    cfg = {**dotenv_values('.env'),**os.environ}
    return dict(host=cfg.get('DB_HOST','localhost'),port=int(cfg.get('DB_PORT',5432)),
                user=cfg.get('DB_USER','postgres'),password=cfg.get('DB_PASSWORD',''),
                database=cfg.get('DB_NAME','arti_bot'))


async def verify_copy():
    """A consistent local copy, with no provider calls or working database writes."""
    from database import connection
    if connection._pool is not None:
        raise RuntimeError('Run copy verification in a separate maintenance process')
    params = parameters()
    name = 'arti_cognition_copy_'+uuid.uuid4().hex
    assert re.fullmatch(r'arti_cognition_copy_[0-9a-f]{32}',name)
    source = await asyncpg.connect(**params)
    target = None
    created = False
    try:
        await source.execute(f'CREATE DATABASE "{name}"')
        created = True
        target = await asyncpg.create_pool(**{**params,'database':name},min_size=1,max_size=5)
        async with target.acquire() as conn:
            await connection.create_tables(conn)
        coverage = {}
        async with source.transaction(isolation='repeatable_read',readonly=True):
            for table in TABLES:
                if not await source.fetchval('SELECT to_regclass($1)',table):
                    continue
                async with target.acquire() as conn:
                    columns = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position",table)
                    # Provider embeddings are derivative indexes, regenerated.
                    names = [r['column_name'] for r in columns if r['column_name']!='embedding']
                    source_names = set(await source.fetchval("SELECT ARRAY_AGG(column_name) FROM information_schema.columns WHERE table_schema='public' AND table_name=$1",table))
                    names = [n for n in names if n in source_names]
                    records = await source.fetch('SELECT '+','.join('"'+n+'"' for n in names)+f' FROM {table}')
                    if records:
                        await conn.copy_records_to_table(table,records=[tuple(r) for r in records],columns=names)
                    coverage[table] = len(records)
                    if 'id' in names:
                        await conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'),COALESCE((SELECT MAX(id) FROM {table}),1),(SELECT COUNT(*)>0 FROM {table}))")
        await ensure_schema(target)
        first = await HistoricalMigration(target).run(max_rows=11)
        completed = await HistoricalMigration(target).run()
        again = await HistoricalMigration(target).run()
        if not completed['complete'] or again['imported_this_run']:
            raise RuntimeError('Migration coverage/idempotence check failed')
        report = dict(source_counts=coverage,partial=first,completed=completed,repeated=again,
                      metrics=await operational_metrics(target),source_access='read-only consistent transaction',
                      private_payload_exported=False,provider_calls=0)
        Path('docs/evaluation/migration_copy.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        return report
    finally:
        if target:
            await target.close()
        if created:
            await source.execute(f'DROP DATABASE "{name}"')
        await source.close()


async def run(args):
    if args.command=='verify-copy':
        return await verify_copy()
    pool = await asyncpg.create_pool(**parameters(),min_size=1,max_size=5)
    try:
        await ensure_schema(pool)
        if args.command=='status':
            return await operational_metrics(pool)
        if args.command=='retry-rebuild':
            from cognition.forgetting import schedule_rebuild
            async with pool.acquire() as conn:
                owner = await conn.fetchval('''SELECT MIN(e.owner_id) FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
                    WHERE c.id=$1 AND c.rebuilding''',args.context_id)
            if owner is None:
                raise ValueError('No blocked, owned context to rebuild')
            return dict(job_id=await schedule_rebuild(pool,args.context_id,owner),transport_calls=0)
        if args.command=='migrate':
            return await HistoricalMigration(pool).run(batch_size=args.batch_size,max_rows=args.max_rows)
        if args.command=='authority':
            context = ContextKey('arti',args.chat_id,args.mode,args.scene_id,args.topic_id)
            async with pool.acquire() as conn:
                cid = await conn.fetchval('SELECT id FROM cognitive_contexts WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4 AND topic_id=$5',*context.identity())
            if cid is None:
                raise ValueError('Unknown context: observe/migrate first')
            await CognitiveRuntime(pool,None,'legacy').set_authority(cid,args.value)
            return dict(context_id=cid,authority=args.value,transport_calls=0)
        if args.command=='cancel-unknown':
            async with pool.acquire() as conn:
                count = await conn.execute("UPDATE cognitive_outbox SET status='cancelled',payload=NULL,updated_at=NOW() WHERE id=$1 AND status='delivery_unknown'",args.outbox_id)
            return dict(result=count,automatic_resend=False)
    finally:
        await pool.close()


if __name__=='__main__':
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    sub.add_parser('verify-copy')
    rebuild = sub.add_parser('retry-rebuild')
    rebuild.add_argument('--context-id',type=int,required=True)
    migrate = sub.add_parser('migrate')
    migrate.add_argument('--batch-size',type=int,default=200)
    migrate.add_argument('--max-rows',type=int,default=0)
    authority = sub.add_parser('authority')
    authority.add_argument('--chat-id',type=int,required=True)
    authority.add_argument('--mode',choices=('default','rp'),default='default')
    authority.add_argument('--scene-id',default='')
    authority.add_argument('--topic-id',type=int,default=-1)
    authority.add_argument('--value',choices=('shadow','active','legacy'),required=True)
    reconcile = sub.add_parser('cancel-unknown')
    reconcile.add_argument('--outbox-id',type=int,required=True)
    print(json.dumps(asyncio.run(run(parser.parse_args())),ensure_ascii=True))
