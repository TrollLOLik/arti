"""Initiative in Arti's own life (H4c: I1, I3, I6).

I1  Consent is asked once, in character, inside an ordinary light conversation
    after a few days of acquaintance. A short clear answer becomes the same
    explicit channel preference the interpreter would record; silence is never
    consent. Without a known timezone she asks the local time once and keeps
    only the derived UTC offset, for quiet hours.
I3  New occasions besides expected-event follow-ups: a quiet hello after 7-14
    days of silence (at most monthly, no guilt) and sharing something she
    actually read (I7) when it matches the person's interests, or, for a close
    acquaintance, simply her own thought about it.
I6  Limits learn receptivity from outcomes: answered initiatives widen the
    weekly budget, ignored ones narrow it, two or three ignored in a row pause
    initiatives for weeks. Explicit reminders are not affected.

All sends reuse the receipt/outbox pipeline with a durable per-occasion key,
the shared unsolicited-delivery limits and a final guard under the context lock.
"""
import asyncio
import hashlib
import logging
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from cognition.serialization import load_event, object_value

logger = logging.getLogger(__name__)

CONSENT_MARK = 'Скажи «нет» — и не буду.'
CONSENT_VARIANTS = (
    'Кстати, можно я иногда буду писать тебе первой — если вспомню о тебе или наткнусь на что-то интересное? ' + CONSENT_MARK,
    'Позволишь мне иногда писать первой? Без повода, просто если вспомню о тебе или прочитаю что-то стоящее. ' + CONSENT_MARK,
    'Можно я буду изредка писать тебе сама — когда найду что-то интересное или просто вспомню? ' + CONSENT_MARK,
)
TIME_MARK = 'чтобы не писать тебе среди ночи'
TIME_VARIANTS = (
    'Который у тебя сейчас час? Хочу знать, ' + TIME_MARK + '.',
    'Подскажи, сколько у тебя сейчас времени? Это ' + TIME_MARK + '.',
)
RETURN_VARIANTS = (
    'Давно тебя не слышала. Надеюсь, у тебя всё хорошо — если захочется поболтать, я здесь.',
    'Вспомнила о тебе. Ничего срочного — просто надеюсь, что у тебя всё в порядке.',
    'Давно не болтали. Без повода: просто захотелось узнать, как ты. Отвечать не обязательно.',
)

YES = re.compile(r'^(да|ага|угу|конечно|разумеется|можно|давай|пиши|буду рад[а]?|буду только рад[а]?|ок|окей|хорошо|почему бы и нет|'
                 r'да,? (конечно|можно|пиши|давай)|конечно,? (можно|пиши))[\s!.)]*$')
NO = re.compile(r'^(нет|не надо|не нужно|лучше не( надо)?|не стоит|нет,? (не надо|спасибо|не пиши)|не пиши)[\s!.)]*$')
STOP = re.compile(r'^(стоп|хватит|не пиши мне( первой| сама)?|не надо (мне )?писать( первой)?|хватит (мне )?писать|'
                  r'перестань (мне )?писать|больше не пиши)[\s!.)]*$')
ANSWER_LIMIT = 60

MIN_MESSAGES = 10
MIN_DAYS = 2
QUIET_CONVERSATION = timedelta(minutes=30)
ANSWER_WINDOW = timedelta(hours=48)
RETURN_MIN, RETURN_MAX = timedelta(days=7), timedelta(days=14)
RETURN_EVERY = timedelta(days=30)
SHARE_EVERY = timedelta(days=7)
SHARE_NOTE_AGE = timedelta(days=3)
SHARE_ACTIVE = timedelta(days=30)
TICK_SECONDS = 300


def enabled():
    return os.getenv('ARTI_INITIATIVES_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _norm(text):
    return ' '.join(str(text or '').casefold().replace('ё', 'е').split())


def _variant(seed, variants):
    return variants[int.from_bytes(hashlib.sha256(str(seed).encode()).digest()[:4], 'big') % len(variants)]


def consent_answer(text):
    """True/False for a short clear answer to the consent question, else None."""
    t = _norm(text)
    if not t or len(t) > ANSWER_LIMIT:
        return None
    if NO.match(t) or STOP.match(t):
        return False
    return True if YES.match(t) else None


def stop_request(text):
    t = _norm(text)
    return bool(t) and len(t) <= ANSWER_LIMIT and bool(STOP.match(t) or NO.match(t))


_WORD_HOURS = {'полночь': 0, 'полдень': 12}


def parse_local_time(text, now):
    """UTC offset in minutes from a stated local clock time; None if unclear."""
    t = _norm(text)
    if not t or len(t) > 40:
        return None
    hour = minute = None
    for word, value in _WORD_HOURS.items():
        if word in t:
            hour, minute = value, 0
    m = re.search(r'\b(\d{1,2})[:.](\d{2})\b', t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    elif hour is None:
        m = re.search(r'\b(\d{1,2})\s*(ч|час|часа|часов)?\b', t)
        if m:
            hour, minute = int(m.group(1)), 0
    if hour is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    if re.search(r'вечера|дня', t) and 1 <= hour <= 11:
        hour += 12
    if 'ночи' in t and hour == 12:
        hour = 0
    now = now.astimezone(timezone.utc)
    offset = (hour * 60 + minute) - (now.hour * 60 + now.minute)
    offset = (offset + 720) % 1440 - 720  # nearest representation
    offset = int(round(offset / 15) * 15)
    if offset < -720:
        offset += 1440
    return offset if -720 <= offset <= 840 else None


async def previous_delivered(conn, cid, owner, eid):
    """Arti's message immediately preceding this user event, if it is the last one."""
    row = await conn.fetchrow('''SELECT * FROM cognitive_events WHERE context_id=$1 AND owner_id=$2
        AND origin IN ('user','delivered_action') AND suppressed_at IS NULL AND id<$3
        ORDER BY id DESC LIMIT 1''', cid, owner, eid)
    return row if row and row['origin'] == 'delivered_action' else None


def _text(row):
    return (object_value(row['payload']) or {}).get('text', '') if row and row['payload'] else ''


async def _was_initiative(conn, cid, row):
    """The delivered message was an unsolicited initiative or follow-up."""
    try:
        message_id = int(row['source_id'].split(':')[2])
    except (IndexError, ValueError, AttributeError):
        return False
    return bool(await conn.fetchval('''SELECT 1 FROM cognitive_outbox WHERE context_id=$1 AND receipt_id=$2
        AND (delivery_key LIKE 'initiative:%' OR delivery_key LIKE 'intention%')''', cid, message_id))


async def consent_patch(conn, cid, event, eid, perception):
    """Turn a clear answer to the consent question (or a stop after an initiative)
    into an explicit proactive preference, exactly as an explicit statement would."""
    from cognition.situations import Situation, SourceSpan
    if event.evidence.origin.value != 'user' or event.context.mode != 'default' or event.context.chat_id <= 0:
        return perception
    previous = await previous_delivered(conn, cid, event.evidence.owner_id, eid)
    if previous is None:
        return perception
    value = None
    if CONSENT_MARK in _text(previous):
        value = consent_answer(event.text)
    elif stop_request(event.text) and await _was_initiative(conn, cid, previous):
        value = False
    if value is None or not event.text:
        return perception
    situation = Situation(topic='сообщения первой', kind='preference', modality='interaction',
                          intention_evidence='explicit', outcome='unknown',
                          spans=(SourceSpan(0, len(event.text), event.text),),
                          preferences={'proactive': value}, social_signal='contact')
    return replace(perception, appraisals=(), situation=situation)


async def learn_local_time(memory, conn, cid, eid, event):
    """An answer to the local-time question becomes a forgettable UTC offset."""
    from cognition.memory_repository import key
    if event.context.mode != 'default' or event.context.chat_id <= 0:
        return None
    previous = await previous_delivered(conn, cid, event.evidence.owner_id, eid)
    if previous is None or TIME_MARK not in _text(previous):
        return None
    offset = parse_local_time(event.text, event.observed_at)
    if offset is None:
        return None
    return await memory._put(conn, cid, 'local_time', key('local_time', event.evidence.owner_id),
                             dict(offset_minutes=offset, stated=event.text[:40], at=event.observed_at.isoformat()),
                             event.evidence.owner_id, [eid])


async def local_offset(conn, cid, owner):
    """The person's stated local offset in minutes, if any (owner-scoped, current epoch)."""
    value = await conn.fetchval('''SELECT a.payload FROM cognitive_artifacts a JOIN cognitive_contexts c ON c.id=a.context_id
        WHERE a.context_id=$1 AND a.owner_id=$2 AND a.kind='local_time' AND a.suppressed_at IS NULL
        AND a.payload IS NOT NULL AND a.projection_epoch=c.suppression_epoch
        AND NOT EXISTS(SELECT 1 FROM cognitive_provenance p JOIN cognitive_events e ON e.id=p.source_event_id
            WHERE p.artifact_id=a.id AND (e.suppressed_at IS NOT NULL OR e.owner_id IS DISTINCT FROM a.owner_id))
        ORDER BY a.id DESC LIMIT 1''', cid, owner)
    if value is None:
        return None
    offset = object_value(value).get('offset_minutes')
    return offset if isinstance(offset, int) else None


async def permission(conn, cid, owner):
    """The current explicit proactive preference (True/False) or None if never stated."""
    value = await conn.fetchval('''SELECT a.payload FROM cognitive_artifacts a JOIN cognitive_contexts c ON c.id=a.context_id
        WHERE a.context_id=$1 AND a.owner_id=$2 AND a.kind='relationship' AND a.suppressed_at IS NULL
        AND a.projection_epoch=c.suppression_epoch''', cid, owner)
    if value is None:
        return None
    preference = (object_value(value).get('preferences') or {}).get('proactive')
    return preference if isinstance(preference, bool) else None


async def _asked(conn, cid, owner, mark):
    return bool(await conn.fetchval('''SELECT 1 FROM cognitive_events WHERE context_id=$1 AND owner_id=$2
        AND origin='delivered_action' AND suppressed_at IS NULL AND strpos(payload->>'text',$3)>0 LIMIT 1''', cid, owner, mark))


async def question_due(runtime, turn, *, light):
    """At most one extra in-character question after a light private reply."""
    event = turn.event
    if (not light or event.evidence.origin.value != 'user' or event.context.mode != 'default'
            or event.context.chat_id <= 0 or event.audience.kind in ('group', 'topic')):
        return None
    plan = turn.expression
    if plan is not None and (plan.serious or getattr(plan, 'rupture', 0.) >= .1):
        return None
    cid, owner = turn.context_id, event.evidence.owner_id
    async with runtime.pool.acquire() as conn:
        allowed = await permission(conn, cid, owner)
        if allowed is None:
            stats = await conn.fetchrow('''SELECT count(*) AS messages,
                count(DISTINCT (observed_at AT TIME ZONE 'UTC')::date) AS days FROM cognitive_events
                WHERE context_id=$1 AND owner_id=$2 AND origin='user' AND suppressed_at IS NULL''', cid, owner)
            if stats['messages'] >= MIN_MESSAGES and stats['days'] >= MIN_DAYS and not await _asked(conn, cid, owner, CONSENT_MARK):
                return 'consent'
            return None
        if allowed:
            known = await conn.fetchval('SELECT timezone FROM arti_organizer_preferences WHERE owner_id=$1', owner)
            if not known and await local_offset(conn, cid, owner) is None and not await _asked(conn, cid, owner, TIME_MARK):
                return 'time'
    return None


def question_text(kind, seed):
    return _variant(seed, CONSENT_VARIANTS if kind == 'consent' else TIME_VARIANTS)


async def receptivity(conn, cid, now):
    """Outcomes of recent initiatives in this context (I6)."""
    rows = await conn.fetch('''SELECT o.updated_at AS sent_at, EXISTS(SELECT 1 FROM cognitive_events e
            WHERE e.context_id=o.context_id AND e.origin='user' AND e.suppressed_at IS NULL
            AND e.observed_at>o.updated_at AND e.observed_at<=o.updated_at+make_interval(secs=>$3)) AS answered
        FROM cognitive_outbox o WHERE o.context_id=$1 AND o.status='delivered' AND o.delivery_key LIKE 'initiative:%'
        AND o.updated_at>$2 ORDER BY o.updated_at DESC LIMIT 5''', cid, now - timedelta(days=90), ANSWER_WINDOW.total_seconds())
    pending = any(not r['answered'] and now - r['sent_at'] < ANSWER_WINDOW for r in rows)
    settled = [r for r in rows if r['answered'] or now - r['sent_at'] >= ANSWER_WINDOW]
    ignored = 0
    for r in settled:
        if r['answered']:
            break
        ignored += 1
    recent = settled[:3]
    warm = len(recent) >= 2 and sum(r['answered'] for r in recent) >= 2
    budget = 1 if ignored == 1 else 3 if warm else 2
    paused_until = None
    if ignored >= 2 and settled:
        paused_until = settled[0]['sent_at'] + (timedelta(days=30) if ignored >= 3 else timedelta(days=14))
    week = sum(1 for r in rows if now - r['sent_at'] < timedelta(days=7))
    return dict(pending=pending, ignored_in_a_row=ignored, weekly_budget=budget, sent_this_week=week,
                paused_until=paused_until, allowed=not pending and week < budget and (paused_until is None or now >= paused_until))


def _tokens(text):
    return {w for w in re.findall(r'\w{5,}', _norm(text))}


async def _interests(runtime, cid, owner):
    beliefs = await runtime.memory.artifacts(cid, owner, 'belief', limit=64)
    episodes = await runtime.memory.artifacts(cid, owner, 'episode', limit=32)
    words = set()
    for row in beliefs:
        p = row['payload']
        if p.get('subject') == owner and p.get('status') == 'current':
            words |= _tokens(f"{p.get('predicate', '')} {p.get('value', '')}".replace('_', ' '))
    for row in episodes:
        words |= _tokens(row['payload'].get('topic', ''))
    return words


def share_text(note, matched):
    head = f'Сегодня читала «{note["title"]}». {note["opinion"]}'
    if matched:
        return f'{head} Подумала о тебе — кажется, это по твоей части: {note["url"]}'
    return f'{head} {note["url"]}'


async def _sent(conn, cid, prefix, since):
    return bool(await conn.fetchval('''SELECT 1 FROM cognitive_outbox WHERE context_id=$1 AND starts_with(delivery_key,$2)
        AND status IN ('delivered','sending','delivery_unknown') AND updated_at>$3 LIMIT 1''', cid, prefix, since))


async def evaluate(runtime, conn, ctx, now, notes=(), interests=frozenset()):
    """One occasion for this private context, or None. Read-only, on the caller's connection."""
    from cognition.initiative_policy import private_reason
    from cognition.relationships import relationship_view
    cid, owner = ctx['id'], ctx['chat_id']
    if (ctx['mode'] != 'default' or ctx['topic_id'] != -1 or owner <= 0 or ctx['authority'] != 'active' or ctx['rebuilding']):
        return None
    if await permission(conn, cid, owner) is not True:
        return None
    if not await conn.fetchval('SELECT enabled FROM response_status WHERE chat_id=$1', owner):
        return None
    if await private_reason(conn, owner, now, context_id=cid):
        return None
    last_any = await conn.fetchrow('''SELECT id,observed_at FROM cognitive_events WHERE context_id=$1 AND owner_id=$2
        AND origin IN ('user','delivered_action') AND suppressed_at IS NULL ORDER BY id DESC LIMIT 1''', cid, owner)
    last_user = await conn.fetchrow('''SELECT id,observed_at FROM cognitive_events WHERE context_id=$1 AND owner_id=$2
        AND origin='user' AND suppressed_at IS NULL AND payload IS NOT NULL ORDER BY id DESC LIMIT 1''', cid, owner)
    if not last_any or not last_user or now - last_any['observed_at'] < QUIET_CONVERSATION:
        return None
    relationship = await conn.fetchval('''SELECT payload FROM cognitive_artifacts WHERE context_id=$1 AND owner_id=$2
        AND kind='relationship' AND suppressed_at IS NULL AND projection_epoch=$3''', cid, owner, ctx['suppression_epoch'])
    view = relationship_view(object_value(relationship), now) if relationship else None
    if view is None or view.get('rupture'):
        return None
    limits = await receptivity(conn, cid, now)
    if not limits['allowed']:
        return None
    base = dict(context_id=cid, owner=owner, source_id=last_user['id'], last_event_id=last_any['id'],
                epoch=ctx['suppression_epoch'])
    silence = now - last_user['observed_at']
    if RETURN_MIN <= silence <= RETURN_MAX and not await _sent(conn, cid, 'initiative:return:', now - RETURN_EVERY):
        key = f'initiative:return:{owner}:{last_user["id"]}'
        return dict(base, kind='return', key=key, text=_variant(key, RETURN_VARIANTS))
    if silence > SHARE_ACTIVE or await _sent(conn, cid, 'initiative:reading:', now - SHARE_EVERY):
        return None
    # After a recent loss, threat or conflict an article is not a kind gesture.
    if await conn.fetchval('''SELECT 1 FROM cognitive_artifacts WHERE context_id=$1 AND owner_id=$2 AND kind='concern'
            AND suppressed_at IS NULL AND payload->>'status'='open' AND (payload->>'since')::timestamptz>$3 LIMIT 1''',
            cid, owner, now - timedelta(days=14)):
        return None
    close = view['dimensions']['familiarity']['value'] >= .85
    for note in notes:
        if now - note['created_at'] > SHARE_NOTE_AGE:
            continue
        prefix = f'initiative:reading:{owner}:{note["id"]}:'
        if await _sent(conn, cid, prefix, now - timedelta(days=365)):
            continue
        matched = bool(interests & _tokens(f'{note["topic"]} {note["title"]} {note["gist"]}'))
        if matched or close:
            return dict(base, kind='reading', key=prefix.rstrip(':'), text=share_text(note, matched), note_id=note['id'],
                        interests=frozenset(interests))
    return None


async def due(runtime, now=None):
    from cognition.reading import recent_notes
    from cognition.types import MODEL_VERSION
    now = now or runtime.clock()
    notes = await recent_notes(runtime.pool, now, days=3)
    async with runtime.pool.acquire() as conn:
        contexts = await conn.fetch('''SELECT c.* FROM cognitive_contexts c WHERE c.mode='default' AND c.topic_id=-1
            AND c.chat_id>0 AND c.authority='active' AND NOT c.rebuilding AND c.model_version=$1
            AND EXISTS(SELECT 1 FROM cognitive_artifacts a WHERE a.context_id=c.id AND a.owner_id=c.chat_id
                AND a.kind='relationship' AND a.suppressed_at IS NULL AND a.payload->'preferences'->>'proactive'='true')
            ORDER BY c.id''', MODEL_VERSION)
    found = []
    for ctx in contexts:
        # Interests read permitted memory on their own connections, outside the evaluation.
        interests = await _interests(runtime, ctx['id'], ctx['chat_id']) if notes else frozenset()
        async with runtime.pool.acquire() as conn:
            candidate = await evaluate(runtime, conn, ctx, now, notes, interests)
        if candidate:
            found.append(candidate)
    return found


async def send(runtime, bot, candidate):
    """Deliver one occasion through the receipt pipeline; the guard re-checks under the lock."""
    from cognition.affect import expression
    from cognition.delivery import send_with_receipt, DeliverySuppressed, DeliveryUnknown
    from cognition.reading import recent_notes
    from cognition.runtime import PreparedTurn, CURRENT_TURN
    cid, owner = candidate['context_id'], candidate['owner']
    async with runtime.pool.acquire() as conn:
        source = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',
                                     cid, candidate['source_id'])
    if not source:
        return False
    event = load_event(source['payload'])
    state = await runtime.personal_state(cid, owner)
    turn = PreparedTurn(runtime, cid, source['id'], event, expression(state), '', candidate['epoch'], 'active')
    turn.event = replace(event, event_id=candidate['key'])
    turn.supporting_event_ids = (source['id'],)
    turn.initiative = dict(owner_id=owner, context_daily=1, spacing_seconds=6 * 3600)

    async def guard(turn, conn):
        # Same connection and lock as the outbox insert: no nested pool use.
        ctx = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1', cid)
        notes = await recent_notes(runtime.pool, runtime.clock(), days=3, conn=conn) if candidate['kind'] == 'reading' else ()
        current = await evaluate(runtime, conn, ctx, runtime.clock(), notes, candidate.get('interests', frozenset()))
        return bool(current and current['key'] == candidate['key'] and current['last_event_id'] == candidate['last_event_id'])
    turn.delivery_guard = guard
    token = CURRENT_TURN.set(turn)
    try:
        from bot.retry_bot import RetryBot
        if isinstance(bot, RetryBot):
            await bot.send_message(chat_id=owner, text=candidate['text'])
        else:
            await send_with_receipt(bot.send_message, (), dict(chat_id=owner, text=candidate['text']), 'message')
        return True
    except (DeliverySuppressed, DeliveryUnknown):
        return False
    finally:
        CURRENT_TURN.reset(token)


async def run_initiative_cycle(runtime, bot):
    sent = 0
    for candidate in await due(runtime):
        try:
            sent += bool(await send(runtime, bot, candidate))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('initiative_skipped')
    return sent


async def initiative_scheduler(runtime, bot):
    while True:
        if enabled() and runtime is not None and runtime.mode != 'legacy':
            try:
                await run_initiative_cycle(runtime, bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('initiative_cycle_failed')
        await asyncio.sleep(TICK_SECONDS)
