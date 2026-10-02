"""Bound foreground intake without pretending it is already durable."""
import asyncio
import contextvars
import functools
import logging
import os

ACCEPTED_REQUESTS=contextvars.ContextVar('arti_intake_accepted',default=None)
logger=logging.getLogger(__name__)


def note_accepted(id):
    pending=ACCEPTED_REQUESTS.get()
    if pending is not None and id not in pending: pending.append(id)


def bounded_intake(callback, *, seconds=None):
    @functools.wraps(callback)
    async def wrapped(update, context):
        budget=seconds if seconds is not None else float(os.getenv('ARTI_INTAKE_BUDGET_SECONDS','180'))
        if not 0 < budget <= 900: raise ValueError('invalid_intake_timeout')
        accepted=[]; token=ACCEPTED_REQUESTS.set(accepted)
        try:
            async with asyncio.timeout(budget):
                return await callback(update, context)
        except TimeoutError:
            logger.info('intake_deadline',extra={'arti_event':'intake_deadline'})
            message=getattr(update,'effective_message',None)
            if message is not None:
                text=(f'Запрос {accepted[-1]} сохранён. Подготовка входящего сообщения прервана по времени; статус: /request {accepted[-1]}.'
                      if accepted else 'Не удалось вовремя подтвердить подготовку сообщения. Проверь /request перед повторной отправкой.')
                # Cosmetic notice only. No RetryBot retry or implied successful result.
                try:
                    from telegram.ext import ExtBot
                    kwargs={'chat_id':update.effective_chat.id,'text':text}
                    if getattr(message,'message_thread_id',None): kwargs['message_thread_id']=message.message_thread_id
                    async with asyncio.timeout(3):
                        await ExtBot.send_message(context.bot,**kwargs)
                except Exception: pass
        finally:
            ACCEPTED_REQUESTS.reset(token)
    return wrapped
