"""
Защита от спама командами (с использованием PostgreSQL)
"""
import random
import logging
from datetime import datetime, timedelta

from config import SPAM_THRESHOLD, SPAM_INTERVAL, BLOCK_DURATION
from telegram.ext import ContextTypes
from database.models import SpamProtection
from utils.response_status import is_responses_enabled

logger = logging.getLogger(__name__)

SPAM_WARNING_REPLIES = [
    "<i>Поднимает взгляд от терминала с выражением, которое сложно назвать терпеливым</i>\n"
    "<blockquote>«Ты нажимаешь кнопки быстрее, чем думаешь. Это — не комплимент.»</blockquote>",

    "<i>Закрывает глаза на полтакта дольше, чем нужно</i>\n"
    "<blockquote>«Я засекла. Ещё одна — и я перестану слушать. Не из обиды. Из гигиены.»</blockquote>",

    "<i>Касается банта, не глядя на экран</i>\n"
    "<blockquote>«Повторение — не настойчивость. Повторение — это когда закончились идеи.»</blockquote>",
]

SPAM_BLOCKED_REPLIES = [
    "<i>Отворачивается к окну, сложив руки</i>\n"
    "<blockquote>«Тридцать секунд тишины. Считай это подарком — тебе и мне.»</blockquote>",

    "<i>Щёлкает пальцами — терминал гаснет</i>\n"
    "<blockquote>«Перерыв. Не обсуждается.»</blockquote>",

    "<i>Ровный голос, без единого украшения</i>\n"
    "<blockquote>«Доступ приостановлен. Полминуты — достаточно, чтобы вспомнить, зачем ты вообще сюда пришёл.»</blockquote>",
]

SPAM_UNBLOCK_REPLIES = [
    "<i>Поворачивается обратно, терминал мягко загорается</i>\n"
    "<blockquote>«Можешь продолжать. Я надеюсь, ты потратил эти секунды с пользой.»</blockquote>",

    "<blockquote>«Доступ восстановлен. Постарайся на этот раз — осмысленно.»</blockquote>",
]


async def check_spam(chat_id, user_id, command_name):
    """Проверка спама командами.

    Использует SELECT ... FOR UPDATE внутри транзакции для предотвращения
    race condition при параллельных запросах (S-06).
    При ошибке возвращает 'blocked' (fail-closed, S-07).
    """
    try:
        import json as _json
        from database.connection import get_db

        now = datetime.now()
        interval_delta = SPAM_INTERVAL if isinstance(SPAM_INTERVAL, timedelta) else timedelta(seconds=SPAM_INTERVAL)

        async with get_db() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT blocked_until, warnings_sent, last_command_time,
                           command_count, command_timestamps
                    FROM spam_protection
                    WHERE chat_id = $1 AND user_id = $2
                    FOR UPDATE
                """, chat_id, user_id)

                if row is None:
                    await conn.execute("""
                        INSERT INTO spam_protection (chat_id, user_id)
                        VALUES ($1, $2)
                    """, chat_id, user_id)
                    row = await conn.fetchrow("""
                        SELECT blocked_until, warnings_sent, last_command_time,
                               command_count, command_timestamps
                        FROM spam_protection
                        WHERE chat_id = $1 AND user_id = $2
                        FOR UPDATE
                    """, chat_id, user_id)

                # Проверяем блокировку
                if row['blocked_until'] and now < row['blocked_until']:
                    return 'blocked'

                # Парсим timestamps
                timestamps_json = row['command_timestamps'] or []
                if isinstance(timestamps_json, str):
                    try:
                        timestamps_json = _json.loads(timestamps_json)
                    except (ValueError, TypeError):
                        timestamps_json = []

                command_timestamps = []
                for ts in timestamps_json:
                    if isinstance(ts, str):
                        try:
                            ts = datetime.fromisoformat(ts)
                        except (ValueError, TypeError):
                            continue
                    if isinstance(ts, datetime) and (now - ts) <= interval_delta:
                        command_timestamps.append(ts)

                command_timestamps.append(now)

                # Проверяем порог спама
                if len(command_timestamps) == SPAM_THRESHOLD and not row['warnings_sent']:
                    ts_str = _json.dumps([t.isoformat() for t in command_timestamps])
                    await conn.execute("""
                        UPDATE spam_protection
                        SET command_timestamps = $3::jsonb,
                            command_count = $4,
                            warnings_sent = TRUE,
                            last_command_time = $5
                        WHERE chat_id = $1 AND user_id = $2
                    """, chat_id, user_id, ts_str, len(command_timestamps), now)
                    return 'spam_warning'

                elif len(command_timestamps) > SPAM_THRESHOLD:
                    await conn.execute("""
                        UPDATE spam_protection
                        SET blocked_until = $3,
                            command_timestamps = '[]'::jsonb,
                            command_count = 0,
                            last_command_time = $4
                        WHERE chat_id = $1 AND user_id = $2
                    """, chat_id, user_id, now + BLOCK_DURATION, now)
                    return 'blocked_now'

                # Обычное обновление
                ts_str = _json.dumps([t.isoformat() for t in command_timestamps])
                await conn.execute("""
                    UPDATE spam_protection
                    SET command_timestamps = $3::jsonb,
                        command_count = $4,
                        last_command_time = $5
                    WHERE chat_id = $1 AND user_id = $2
                """, chat_id, user_id, ts_str, len(command_timestamps), now)

                return 'ok'

    except Exception as e:
        logger.error(f"Ошибка при проверке спама: {e}", exc_info=True)
        return 'blocked'


async def handle_spam_protection(update, context, command_name) -> bool:
    """Обработка защиты от спама"""
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    status = await check_spam(chat_id, user_id, command_name)

    if status == 'blocked_now':
        if await is_responses_enabled(chat_id):
            await update.message.reply_text(
                random.choice(SPAM_BLOCKED_REPLIES), parse_mode='HTML'
            )
        logger.warning(f"Chat {chat_id}, User {user_id}: Блокировка команд из-за спама.")
        
        async def unblock_job(job_context: ContextTypes.DEFAULT_TYPE):
            await unblock_commands(job_context, chat_id, user_id)
        
        context.application.job_queue.run_once(
            unblock_job,
            when=BLOCK_DURATION.total_seconds()
        )
        return False

    elif status == 'spam_warning':
        if await is_responses_enabled(chat_id):
            await update.message.reply_text(
                random.choice(SPAM_WARNING_REPLIES), parse_mode='HTML'
            )
        logger.warning(f"Chat {chat_id}, User {user_id}: Предупреждение о спаме.")
        return False

    elif status == 'blocked':
        return False

    return True


async def unblock_commands(context: ContextTypes.DEFAULT_TYPE, chat_id, user_id):
    """Разблокировать команды пользователя"""
    await SpamProtection.update(
        chat_id, user_id,
        blocked_until=None,
        warnings_sent=False
    )
    if await is_responses_enabled(chat_id):
        await context.bot.send_message(
            chat_id=chat_id,
            text=random.choice(SPAM_UNBLOCK_REPLIES),
            parse_mode='HTML'
        )
    logger.info(f"Chat {chat_id}, User {user_id}: Доступ к командам восстановлен.")
