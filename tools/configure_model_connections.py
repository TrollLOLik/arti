"""Apply the additive connection migration and show only credential-free status.

python -m tools.configure_model_connections --apply
python -m tools.configure_model_connections
"""
import argparse
import asyncio
import json
import os

import asyncpg
from dotenv import dotenv_values

from database.ai_model_schema import migrate_ai_models


async def configure_native_gemini(conn, cfg):
    """One-time import of an existing Google key, never an inference fallback."""
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM ai_models WHERE key='gemini' FOR UPDATE")
        if not row or not row['model'].startswith('gemini'):
            raise ValueError('missing_gemini_catalog_entry')
        url = (row['base_url'] or '').rstrip('/')
        if url and url not in ('https://generativelanguage.googleapis.com',
                'https://generativelanguage.googleapis.com/v1beta',
                'https://generativelanguage.googleapis.com/v1beta/openai'):
            raise ValueError('existing_gemini_endpoint_requires_manual_configuration')
        caps = json.loads(row['capabilities']) if isinstance(row['capabilities'], str) else row['capabilities']
        if row['model'] == 'gemini-3.5-flash-lite' and caps == {'inputs': ['text'], 'features': []}:
            caps = {'inputs': ['text', 'image', 'video'], 'features': ['structured_output', 'search', 'maps']}
        await conn.execute("""UPDATE ai_models SET api_type='gemini',
            base_url='https://generativelanguage.googleapis.com',
            api_key=COALESCE(api_key,$1), capabilities=$2::jsonb WHERE key='gemini'""",
            cfg.get('GEMINI_API_KEY') or cfg.get('GOOGLE_API_KEY') or None, json.dumps(caps))


async def run(apply=False, native_gemini=False):
    cfg = {**dotenv_values('.env'), **os.environ}
    conn = await asyncpg.connect(host=cfg.get('DB_HOST', 'localhost'),
        port=int(cfg.get('DB_PORT', 5432)), database=cfg.get('DB_NAME', 'arti_bot'),
        user=cfg.get('DB_USER', 'postgres'), password=cfg.get('DB_PASSWORD', ''), timeout=10)
    try:
        if apply or native_gemini:
            await migrate_ai_models(conn)
        if native_gemini:
            await configure_native_gemini(conn, cfg)
        migrated = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='ai_models' AND column_name='api_type')""")
        if not migrated:
            print('Connection schema is not applied. Run with --apply.')
            return
        rows = await conn.fetch("""SELECT key, api_type, is_active, is_maintenance,
            NULLIF(btrim(base_url),'') IS NOT NULL AND api_key IS NOT NULL
                AND (api_type <> 'gemini' OR NULLIF(btrim(api_key),'') IS NOT NULL) AS configured
            FROM ai_models ORDER BY created_at,key""")
        print('Model connections (no URLs or credentials):')
        for row in rows:
            print(f"{row['key']}: api_type={row['api_type']}, configured={row['configured']}, active={row['is_active']}, maintenance={row['is_maintenance']}")
        print(f'Total: {len(rows)}. Configured: {sum(row["configured"] for row in rows)}.')
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--native-gemini', action='store_true',
        help='Configure key=gemini for google-genai and import an existing .env key only if the DB key is NULL')
    args = parser.parse_args()
    try:
        asyncio.run(run(args.apply, args.native_gemini))
    except Exception as exc:
        # PostgreSQL/HTTP errors may include connection strings. Keep them private.
        raise SystemExit('Model connection migration failed: ' + type(exc).__name__) from None


if __name__ == '__main__':
    main()
