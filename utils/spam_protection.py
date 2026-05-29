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
    """Проверка спама командами"""
    try:
        now = datetime.now()
        data = await SpamProtection.get_or_create(chat_id, user_id)

        # Проверяем блокировку
        if data['blocked_until'] and now < data['blocked_until']:
            return 'blocked'

        # Очищаем старые времена команд (старше SPAM_INTERVAL)
        interval_delta = SPAM_INTERVAL if isinstance(SPAM_INTERVAL, timedelta) else timedelta(seconds=SPAM_INTERVAL)
        command_timestamps = []
        for ts in data['command_timestamps']:
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except:
                    continue
            if isinstance(ts, datetime) and (now - ts) <= interval_delta:
                command_timestamps.append(ts)
        
        # Добавляем текущее время
        command_timestamps.append(now)

        # Проверяем порог спама
        if len(command_timestamps) == SPAM_THRESHOLD and not data['warnings_sent']:
            await SpamProtection.update(
                chat_id, user_id,
                command_timestamps=command_timestamps,
                command_count=len(command_timestamps),
                warnings_sent=True,
                last_command_time=now
            )
            return 'spam_warning'
        elif len(command_timestamps) > SPAM_THRESHOLD:
            await SpamProtection.update(
                chat_id, user_id,
                blocked_until=now + BLOCK_DURATION,
                command_timestamps=[],
                command_count=0,
                last_command_time=now
            )
            return 'blocked_now'
        
        # Обновляем в БД (одним запросом)
        await SpamProtection.update(
            chat_id, user_id,
            command_timestamps=command_timestamps,
            command_count=len(command_timestamps),
            last_command_time=now
        )
        
        return 'ok'
    except Exception as e:
        logger.error(f"Ошибка при проверке спама: {e}", exc_info=True)
        return 'ok'


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
