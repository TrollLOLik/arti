"""Restart-safe due notifications, bypassing RetryBot's network retry loop."""
import asyncio
import logging
from organizer.repository import Repository
from organizer.time import format_scheduled


def repository():
    from database import connection
    if connection._pool is None: raise RuntimeError('organizer_database_unavailable')
    return Repository(connection._pool)


async def dispatch_once(bot, *, repo=None, sender=None, timeout_seconds=45):
    if not 0<timeout_seconds<=45: raise ValueError('invalid_notification_timeout')
    repo=repo or repository()
    claimed=await repo.claim_due()
    if not claimed: return False
    row=await repo.begin_send(claimed['id'],claimed['token'])
    if not row: return True
    text=('Событие' if row['kind']=='event' else 'Напоминание')+': '+row['title']+'\n'+format_scheduled(row['due_at'],row.get('timezone'))+'\nID: '+row['id']
    try:
        async with asyncio.timeout(timeout_seconds):
            if sender is None:
                from telegram.ext import ExtBot
                # Direct base method: RetryBot.send_message would retry ambiguous IO.
                result=await ExtBot.send_message(bot,chat_id=row['chat_id'],text=text,parse_mode=None,read_timeout=20,write_timeout=20,connect_timeout=10)
            else: result=await sender(chat_id=row['chat_id'],text=text,parse_mode=None)
            await repo.finish_send(row['id'],row['token'],delivered=True,message_id=getattr(result,'message_id',None))
    except asyncio.CancelledError:
        await asyncio.shield(repo.finish_send(row['id'],row['token']))
        raise
    except Exception:
        await repo.finish_send(row['id'],row['token'])
        logging.getLogger(__name__).warning('Organizer notification outcome unknown; automatic retry disabled')
    return True


async def worker(bot):
    cleanup_at=0.
    while True:
        try:
            if asyncio.get_running_loop().time()>=cleanup_at:
                from organizer.natural import cleanup_expired
                await cleanup_expired(repository().pool)
                cleanup_at=asyncio.get_running_loop().time()+60
            ready=await dispatch_once(bot)
        except asyncio.CancelledError: raise
        except Exception:
            logging.getLogger(__name__).warning('Organizer worker deferred')
            ready=False
        await asyncio.sleep(.1 if ready else 2)
