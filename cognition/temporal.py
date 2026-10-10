"""Present-time grounding for a conversational reply.

Only timestamps and an explicitly saved timezone are read, never message
content. Missing data is omitted or stated as unknown rather than guessed: an
unknown personal timezone is never replaced by the host or by Arti's own clock.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Arti's own clock follows the character's lore (Magnitogorsk). It colours her
# manner a little; it is never presented as the person's local time.
ARTI_TIMEZONE = 'Asia/Yekaterinburg'
# User fragments of one burst arrive seconds apart; they are not a previous contact.
BURST_SECONDS = 90
GAP_NOTICE_SECONDS = 30 * 60

WEEKDAYS = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')
MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
          'сентября', 'октября', 'ноября', 'декабря')


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def day_phase(hour):
    if 5 <= hour < 11:
        return 'утро'
    if 11 <= hour < 17:
        return 'день'
    if 17 <= hour < 23:
        return 'вечер'
    return 'ночь'


def humanize_gap(delta):
    seconds = max(0., delta.total_seconds())
    if seconds < 3600:
        n = max(1, int(seconds // 60))
        return f'{n} {plural(n, "минута", "минуты", "минут")}'
    if seconds < 86400:
        n = int(seconds // 3600)
        return f'{n} {plural(n, "час", "часа", "часов")}'
    days = int(seconds // 86400)
    if days < 14:
        return f'{days} {plural(days, "день", "дня", "дней")}'
    if days < 60:
        n = days // 7
        return f'{n} {plural(n, "неделя", "недели", "недель")}'
    if days < 365:
        n = days // 30
        return f'{n} {plural(n, "месяц", "месяца", "месяцев")}'
    return 'больше года'


def _clock(moment):
    return f'{moment:%H:%M}, {WEEKDAYS[moment.weekday()]}, {moment.day} {MONTHS[moment.month - 1]}'


def _zone(name):
    if not name or not isinstance(name, str):
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def temporal_block(now, *, user_timezone=None, private=True, previous_contact=None):
    """Render the [Сейчас] block. ``now`` and ``previous_contact`` must be aware."""
    if now.tzinfo is None:
        raise ValueError('Temporal grounding requires an aware timestamp')
    now = now.astimezone(timezone.utc)
    arti = now.astimezone(ZoneInfo(ARTI_TIMEZONE))
    lines = [f'Твоё время (по образу — Магнитогорск): {_clock(arti)}, {day_phase(arti.hour)}.']
    if private:
        zone = _zone(user_timezone)
        if zone is not None:
            local = now.astimezone(zone)
            lines.append(f'У собеседника: {_clock(local)}, {day_phase(local.hour)}.')
        else:
            lines.append('Часовой пояс собеседника неизвестен — не угадывай его местное время.')
        if previous_contact is not None and previous_contact.tzinfo is not None:
            gap = now - previous_contact.astimezone(timezone.utc)
            if gap.total_seconds() >= GAP_NOTICE_SECONDS:
                lines.append(f'Перерыв с прошлой переписки в этом чате: {humanize_gap(gap)}.')
            else:
                lines.append('Разговор идёт сейчас, без долгого перерыва.')
    lines.append(f'Метки времени в истории диалога указаны по UTC (сейчас {now:%H:%M} UTC).')
    return '[Сейчас]\n' + '\n'.join(lines)


def _private(event):
    audience = getattr(event, 'audience', None)
    if audience is not None and audience.kind in ('group', 'topic'):
        return False
    if audience is not None and audience.kind == 'private':
        return True
    return event.context.chat_id > 0 and event.context.topic_id < 0


async def temporal_context(turn, *, budget=.3):
    """Optional reads for the current turn; any failure omits only that detail."""
    if turn is None or getattr(turn, 'runtime', None) is None:
        return temporal_block(datetime.now(timezone.utc), private=False)
    now = turn.runtime.clock()
    event = turn.event
    private = _private(event)
    user_timezone = previous = None
    if private:
        try:
            async with asyncio.timeout(budget):
                async with turn.runtime.pool.acquire() as conn:
                    user_timezone = await conn.fetchval(
                        'SELECT timezone FROM arti_organizer_preferences WHERE owner_id=$1',
                        event.evidence.owner_id)
                    previous = await conn.fetchval(
                        '''SELECT max(occurred_at) FROM cognitive_events
                           WHERE context_id=$1 AND id<$2 AND suppressed_at IS NULL
                           AND origin IN ('user','delivered_action') AND occurred_at<$3''',
                        turn.context_id, turn.event_id, event.occurred_at - timedelta(seconds=BURST_SECONDS))
        except Exception as exc:  # optional grounding never blocks a reply
            logger.debug('Temporal grounding degraded: %s', type(exc).__name__)
    return temporal_block(now, user_timezone=user_timezone, private=private, previous_contact=previous)
