"""Life between conversations (H4c, I7): Arti reads a little every day.

Once a day a background pass searches the web for one or two materials on
Arti's own interests and keeps a short note: title, real link, gist and her
own opinion. Then "I read today about ..." is true. Notes are public material
plus her opinion; no person's data is used to choose or write them. A note
without a verified link from the search provider is discarded, never invented.
"""
import asyncio
import json
import logging
import os
import re
from datetime import timedelta

from cognition.types import utc

logger = logging.getLogger(__name__)

# Arti's own interests (lore: taste for the beautiful, tea, precise thought, machines).
INTERESTS = (
    ('чай', 'чайная культура, сорта и истории о чае'),
    ('слово', 'язык, поэзия и точные красивые формулировки'),
    ('машины', 'андроиды, роботы и искусственный интеллект'),
    ('решения', 'изящные инженерные и математические решения'),
    ('музыка', 'музыка: новые записи и истории композиторов'),
    ('память', 'наука о памяти, сознании и эмоциях'),
    ('вещи', 'дизайн, архитектура и красиво сделанные вещи'),
    ('небо', 'астрономия и космос'),
)
READS_PER_DAY = 2
NOTE_DAYS = 7
TICK_SECONDS = 3600

SYSTEM = ('Ты — Арти, андроид со вкусом к красивому и точному. Ты читаешь для себя, а не по заданию. '
          'Найди через поиск один содержательный свежий материал (статью, заметку, обзор) на заданную тему '
          'и прочитай его. Ответь строго одним JSON-объектом без пояснений: '
          '{"title": "название материала", "gist": "о чём он, одно-два предложения", '
          '"opinion": "что ты сама об этом думаешь, одно-два предложения от первого лица, в своём характере"}. '
          'Опирайся только на найденное; не выдумывай материалов, фактов и цитат.')


class ReadingUnavailable(Exception):
    pass


def enabled():
    return os.getenv('ARTI_READING_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def topics_for(day):
    """Deterministic rotation: two different interests per day."""
    start = (day.toordinal() * READS_PER_DAY) % len(INTERESTS)
    return [INTERESTS[(start + n) % len(INTERESTS)] for n in range(READS_PER_DAY)]


def parse_note(text, links):
    """Validate the model's note; the link must come from the provider's search metadata."""
    links = [(url, title) for url, title in links if isinstance(url, str) and re.match(r'^https?://', url) and len(url) <= 2048]
    if not links:
        raise ValueError('no_verified_link')
    match = re.search(r'\{.*\}', str(text or ''), re.S)
    if not match:
        raise ValueError('no_note')
    body = json.loads(match.group(0))
    if not isinstance(body, dict) or not {'title', 'gist', 'opinion'} <= set(body):
        raise ValueError('invalid_note')
    note = {}
    for name, limit in (('title', 300), ('gist', 600), ('opinion', 600)):
        value = ' '.join(str(body[name] or '').split())
        if not value or len(value) > limit:
            raise ValueError('invalid_' + name)
        note[name] = value
    note['url'] = links[0][0]
    return note


async def google_search(query, system):
    """Search-grounded generation with the configured model; real links only."""
    from ai.providers.connections import load_model_routes
    from ai.providers.contracts import GenerationRequest
    from ai.providers.google import google_client_for, google_config
    from config import DEFAULT_MODEL
    from google.genai import types
    routes = await load_model_routes(os.getenv('ARTI_READING_MODEL') or DEFAULT_MODEL)
    route = routes.route(GenerationRequest(query, system, (), None, True, False))
    if route.endpoint.provider == 'openai':
        raise ReadingUnavailable('no_verified_search')
    connection = routes.connection_for(route)
    async with google_client_for(connection) as client:
        config = google_config(connection, 2048, .7, system=system,
                               tools=[types.Tool(google_search=types.GoogleSearch())],
                               automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        response = await client.aio.models.generate_content(model=route.endpoint.model, contents=query, config=config)
    links = []
    candidates = getattr(response, 'candidates', None) or []
    metadata = getattr(candidates[0], 'grounding_metadata', None) if candidates else None
    for chunk in getattr(metadata, 'grounding_chunks', None) or []:
        web = getattr(chunk, 'web', None)
        if web is not None and getattr(web, 'uri', None):
            links.append((web.uri, getattr(web, 'title', None) or ''))
    return response.text or '', links


class Reading:
    def __init__(self, runtime, search=None):
        self.runtime = runtime
        self.search = search or google_search

    async def read_today(self):
        """Read the missing topics of today; returns stored notes."""
        today = utc(self.runtime.clock()).date()
        stored = []
        async with self.runtime.pool.acquire() as conn:
            # One reader across bot processes.
            if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended('arti:reading',0))"):
                return stored
            try:
                done = {r['topic'] for r in await conn.fetch('SELECT topic FROM arti_reading_notes WHERE read_on=$1', today)}
                for topic, query in topics_for(today):
                    if topic in done:
                        continue
                    try:
                        text, links = await self.search(f'Тема: {query}. Найди и прочитай один материал.', SYSTEM)
                        note = parse_note(text, links)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Category only: no provider payload in logs; retried next tick.
                        logger.info('reading_skipped: %s', type(exc).__name__)
                        continue
                    row = await conn.fetchrow('''INSERT INTO arti_reading_notes(read_on,topic,title,url,gist,opinion,created_at)
                        VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(read_on,topic) DO NOTHING RETURNING *''',
                        today, topic, note['title'], note['url'], note['gist'], note['opinion'], utc(self.runtime.clock()))
                    if row:
                        stored.append(dict(row))
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended('arti:reading',0))")
        return stored


async def recent_notes(pool, now, days=NOTE_DAYS, limit=3, *, conn=None):
    if conn is None:
        async with pool.acquire() as conn:
            return await recent_notes(pool, now, days, limit, conn=conn)
    rows = await conn.fetch('''SELECT * FROM arti_reading_notes WHERE created_at>$1
        ORDER BY created_at DESC,id DESC LIMIT $2''', utc(now) - timedelta(days=days), limit)
    return [dict(r) for r in rows]


def reading_block(notes, now):
    """Prompt block: what Arti actually read recently (true, with sources)."""
    if not notes:
        return ''
    from cognition.memory_render import ago
    lines = ['[Что ты недавно читала — это правда, можно упомянуть к месту, не навязывая]']
    for note in notes:
        when = ago(note['created_at'], now) or 'сегодня'
        lines.append(f'• {when}: «{note["title"]}» — {note["gist"]} Твоё мнение: {note["opinion"]} ({note["url"]})')
    return '\n'.join(lines)


async def reading_scheduler(runtime):
    service = Reading(runtime)
    while True:
        if enabled() and runtime is not None and runtime.mode != 'legacy':
            try:
                await service.read_today()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('reading_cycle_failed')
        await asyncio.sleep(TICK_SECONDS)
