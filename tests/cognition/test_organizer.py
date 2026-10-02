"""Owned fixtures, disposable PostgreSQL and fake transport only."""
import asyncio
import os
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from organizer.time import OrganizerError,scheduled_at
from organizer.repository import Repository,initialize
from organizer.runtime import dispatch_once


class OrganizerTimeTests(unittest.TestCase):
    now=datetime(2026,1,1,tzinfo=timezone.utc)
    def test_relative_bounds(self):
        self.assertEqual(self.now+timedelta(minutes=30),scheduled_at('30m',now=self.now)[0])
        for value in ('0s','999999d','-10m','tomorrow','2026-03-01'):
            with self.assertRaises(OrganizerError): scheduled_at(value,now=self.now)
    def test_absolute_requires_explicit_zone(self):
        with self.assertRaisesRegex(OrganizerError,'timezone_required'): scheduled_at('2026-03-01T09:00',now=self.now)
        due,label=scheduled_at('2026-03-01T09:00','Europe/Moscow',now=self.now)
        self.assertEqual(6,due.hour); self.assertEqual('Europe/Moscow',label)
    def test_dst_gap_fold_and_explicit_offset(self):
        with self.assertRaisesRegex(OrganizerError,'nonexistent'): scheduled_at('2026-03-29T02:30','Europe/Berlin',now=self.now)
        with self.assertRaisesRegex(OrganizerError,'ambiguous'): scheduled_at('2026-10-25T02:30','Europe/Berlin',now=self.now)
        early=scheduled_at('2026-10-25T02:30+02:00',now=self.now)[0]
        late=scheduled_at('2026-10-25T02:30+01:00',now=self.now)[0]
        self.assertEqual(timedelta(hours=1),late-early)
    def test_due_now_past_and_horizon_rejected(self):
        for value in ('2026-01-01T00:00Z','2025-12-31T23:59Z','2027-01-02T00:00Z'):
            with self.assertRaisesRegex(OrganizerError,'schedule_out_of_range'): scheduled_at(value,now=self.now)


class OrganizerNaturalTests(unittest.IsolatedAsyncioTestCase):
    async def test_recall_and_quoted_request_continue_normal_conversation(self):
        from bot.organizer_commands import handle_natural
        bot=NS(send_message=AsyncMock())
        for text in ('Напомни, что случилось с начальником','Напомни мне имя друга','«Напомни завтра купить чай» — цитата'):
            self.assertFalse(await handle_natural({'chat_id':1,'user_message':text},bot))
        bot.send_message.assert_not_awaited()

    def test_explicit_future_request_is_parsed_for_native_execution(self):
        from organizer.natural import parse
        result=parse('Пожалуйста, напомни мне через 30 минут купить чай')
        self.assertEqual(result,dict(command='remind',action='add',title='купить чай',when='30m'))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class OrganizerSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        async with self.pool.acquire() as conn: await initialize(conn)
        self.repo=Repository(self.pool)
    async def asyncTearDown(self): await self.db.__aexit__(None,None,None)
    async def item(self,key='one',kind='reminder',owner=1):
        return await self.repo.create(owner,owner,kind,'Synthetic title',key,
            due_at=None if kind=='todo' else datetime.now(timezone.utc)+timedelta(minutes=10),timezone_name='UTC')
    async def due(self,id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_organizer_items SET due_at=NOW()-INTERVAL '1 second' WHERE id=$1",id)
    async def expire(self,id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_organizer_items SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1",id)

    async def test_owner_isolation_and_private_only(self):
        a=await self.item(kind='todo'); await self.item(owner=2,kind='todo')
        self.assertEqual(1,len(await self.repo.list(1,1)))
        self.assertIsNone(await self.repo.change(2,2,a['id'],'cancelled'))
        with self.assertRaisesRegex(OrganizerError,'private_chat_required'): await self.repo.list(1,-100)
        with self.assertRaises(OrganizerError): await self.repo.create(1,2,'todo','private','bad')

    async def test_create_dedupe_restart_and_mutation_redelivery(self):
        a,b=await asyncio.gather(self.item(kind='todo'),self.item(kind='todo'))
        self.assertEqual(a['id'],b['id'])
        repo=Repository(self.pool)
        await repo.change(1,1,a['id'],'done',source_key='done')
        await repo.change(1,1,a['id'],'active',source_key='reopen')
        duplicate=await repo.change(1,1,a['id'],'done',source_key='done')
        self.assertEqual('active',duplicate['state'])
        self.assertEqual(1,len(await repo.list(1,1)))

    async def test_future_boundary_and_claim_before_send_restart(self):
        row=await self.item()
        self.assertIsNone(await self.repo.claim_due())
        await self.due(row['id']); old=await self.repo.claim_due()
        await self.expire(row['id']); new=await Repository(self.pool).claim_due()
        self.assertNotEqual(old['token'],new['token'])
        self.assertIsNone(await self.repo.begin_send(old['id'],old['token']))
        self.assertIsNotNone(await self.repo.begin_send(new['id'],new['token']))

    async def test_ambiguous_send_crash_is_not_retried(self):
        row=await self.item(); await self.due(row['id'])
        claimed=await self.repo.claim_due(); await self.repo.begin_send(row['id'],claimed['token'])
        await self.expire(row['id'])
        self.assertIsNone(await Repository(self.pool).claim_due())
        self.assertEqual('delivery_unknown',(await self.repo.list(1,1,include_closed=True))[0]['notification_state'])

    async def test_success_once_and_timeout_once(self):
        a=await self.item(); await self.due(a['id'])
        sender=AsyncMock(return_value=NS(message_id=71))
        self.assertTrue(await dispatch_once(None,repo=self.repo,sender=sender))
        self.assertFalse(await dispatch_once(None,repo=self.repo,sender=sender))
        sender.assert_awaited_once()
        b=await self.item('timeout'); await self.due(b['id'])
        failed=AsyncMock(side_effect=TimeoutError('private error'))
        await dispatch_once(None,repo=self.repo,sender=failed)
        self.assertFalse(await dispatch_once(None,repo=self.repo,sender=failed))
        failed.assert_awaited_once()
        self.assertEqual({'delivered','delivery_unknown'},{r['notification_state'] for r in await self.repo.list(1,1,include_closed=True)})

    async def test_cancellation_before_and_during_notification(self):
        a=await self.item(); await self.due(a['id']); claimed=await self.repo.claim_due()
        await self.repo.change(1,1,a['id'],'cancelled')
        self.assertIsNone(await self.repo.begin_send(a['id'],claimed['token']))
        b=await self.item('inflight'); await self.due(b['id'])
        async def sender(**kwargs):
            await self.repo.change(1,1,b['id'],'cancelled')
            return NS(message_id=8)
        await dispatch_once(None,repo=self.repo,sender=sender)
        rows=await self.repo.list(1,1,include_closed=True)
        self.assertEqual('delivery_unknown',next(r for r in rows if r['id']==b['id'])['notification_state'])
        self.assertIsNone(await self.repo.claim_due())

    async def test_concurrent_workers_claim_single_due_item(self):
        row=await self.item(kind='event'); await self.due(row['id'])
        results=await asyncio.gather(*(self.repo.claim_due() for _ in range(4)))
        self.assertEqual(1,sum(r is not None for r in results))

    async def test_timezone_and_title_validation(self):
        await self.repo.set_timezone(1,1,'Europe/Moscow')
        self.assertEqual('Europe/Moscow',await Repository(self.pool).get_timezone(1,1))
        self.assertIsNone(await self.repo.get_timezone(2,2))
        for title in ('',' '*3,'a'*501):
            with self.assertRaises(OrganizerError): await self.repo.create(1,1,'todo',title,'invalid')
        with self.assertRaises(OrganizerError): await self.repo.set_timezone(1,1,'Not/AZone')

    async def test_structured_commands_create_list_complete_and_cancel(self):
        from bot.organizer_commands import execute
        text=await execute(1,1,'todo',['add','Buy tea'],'cmd1',repo=self.repo)
        row=(await self.repo.list(1,1))[0]
        self.assertIn(row['id'],text)
        self.assertIn('done',await execute(1,1,'todo',['done',row['id']],'cmd2',repo=self.repo))
        self.assertIn('active',await execute(1,1,'todo',['reopen',row['id']],'cmd3',repo=self.repo))
        self.assertIn('cancelled',await execute(1,1,'todo',['cancel',row['id']],'cmd4',repo=self.repo))
        self.assertIn('Записей нет',await execute(2,2,'todo',['list'],'list2',repo=self.repo))
        text=await execute(1,1,'remind',['add','Call','1h'],'cmd5',repo=self.repo)
        reminder=(await self.repo.list(1,1,'reminder'))[0]
        self.assertIn(reminder['id'],text)
        self.assertEqual('Запись не найдена.',await execute(1,1,'todo',['cancel',reminder['id']],'wrong',repo=self.repo))

    async def test_group_command_does_not_read_or_share_private_data(self):
        from bot.organizer_commands import command
        message=NS(text='/todo list',message_id=7,from_user=NS(id=1,is_bot=False),reply_text=AsyncMock())
        update=NS(effective_message=message,effective_user=NS(id=1),effective_chat=NS(id=-100,type='supergroup'))
        with patch('bot.organizer_commands.repository',side_effect=AssertionError('must not access storage')):
            await command(update,NS())
        self.assertIn('личном чате',message.reply_text.call_args.args[0])

    async def test_missing_receipt_is_unknown_not_delivered(self):
        row=await self.item(); await self.due(row['id'])
        await dispatch_once(None,repo=self.repo,sender=AsyncMock(return_value=NS()))
        self.assertEqual('delivery_unknown',(await self.repo.list(1,1,include_closed=True))[0]['notification_state'])

    async def test_timezone_redelivery_does_not_revert_newer_choice(self):
        await self.repo.set_timezone(1,1,'Europe/Moscow',source_key='first-zone')
        await self.repo.set_timezone(1,1,'UTC',source_key='new-zone')
        self.assertEqual('UTC',await self.repo.set_timezone(1,1,'Europe/Moscow',source_key='first-zone'))

    async def test_replayed_creation_after_due_keeps_original_record(self):
        row=await self.item(); await self.due(row['id'])
        repeated=await self.repo.create(1,1,'reminder','Synthetic title','one',due_at=datetime.now(timezone.utc)-timedelta(days=1))
        self.assertEqual(row['id'],repeated['id'])

    async def test_list_reply_fits_telegram_text_budget(self):
        from bot.organizer_commands import execute
        for i in range(21):
            await self.repo.create(1,1,'reminder','a'*500,str(i),due_at=datetime.now(timezone.utc)+timedelta(hours=1))
        text=await execute(1,1,'remind',['list'],'list',repo=self.repo)
        self.assertLessEqual(len(text),4096)
        self.assertEqual(20,text.count('отправка:'))

    async def test_total_transport_deadline_records_unknown_without_retry(self):
        row=await self.item(); await self.due(row['id'])
        async def slow(**kwargs): await asyncio.Event().wait()
        sender=AsyncMock(side_effect=slow)
        await dispatch_once(None,repo=self.repo,sender=sender,timeout_seconds=.01)
        self.assertEqual('delivery_unknown',(await self.repo.list(1,1,include_closed=True))[0]['notification_state'])
        self.assertFalse(await dispatch_once(None,repo=self.repo,sender=sender))
        sender.assert_awaited_once()

    async def test_settled_reminder_and_event_do_not_claim_attendance(self):
        reminder=await self.item('reminder'); await self.due(reminder['id'])
        event=await self.item('event',kind='event'); await self.due(event['id'])
        sender=AsyncMock(return_value=NS(message_id=19))
        await dispatch_once(None,repo=self.repo,sender=sender)
        await dispatch_once(None,repo=self.repo,sender=sender)
        self.assertEqual([],await self.repo.list(1,1))
        rows=await self.repo.list(1,1,include_closed=True)
        self.assertEqual('done',next(r for r in rows if r['id']==reminder['id'])['state'])
        self.assertEqual('active',next(r for r in rows if r['id']==event['id'])['state'])
        async with self.pool.acquire() as conn:
            count=await conn.fetchval("SELECT COUNT(*) FROM arti_organizer_items WHERE owner_id=1 AND state='active' AND (kind='todo' OR notification_state IN ('pending','claimed','sending'))")
        self.assertEqual(0,count)

    async def test_schedule_display_uses_saved_iana_and_explicit_offset(self):
        from organizer.time import format_scheduled
        from bot.organizer_commands import summary
        instant=datetime(2026,10,3,6,0,tzinfo=timezone.utc)
        self.assertIn('09:00:00 +0300 [Europe/Moscow]',format_scheduled(instant,'Europe/Moscow'))
        self.assertIn('01:00:00 -0500 [UTC-05:00]',format_scheduled(instant,'UTC-05:00'))
        row=await self.item()
        row['due_at']=instant; row['timezone']='Europe/Moscow'
        self.assertIn('09:00:00 +0300',summary(row))

class WindowsTimezoneFallbackTests(unittest.TestCase):
    def test_iana_lookup_and_dst_without_system_timezone_database(self):
        import zoneinfo
        from organizer.time import zone,scheduled_at,OrganizerError
        old=zoneinfo.TZPATH
        try:
            zoneinfo.ZoneInfo.clear_cache(); zoneinfo.reset_tzpath([])
            self.assertEqual(zone('Europe/Moscow').key,'Europe/Moscow')
            with self.assertRaisesRegex(OrganizerError,'ambiguous_local_time'):
                scheduled_at('2026-10-25T02:30','Europe/Berlin',now=datetime(2026,1,1,tzinfo=timezone.utc))
            with self.assertRaisesRegex(OrganizerError,'nonexistent_local_time'):
                scheduled_at('2026-03-29T02:30','Europe/Berlin',now=datetime(2026,1,1,tzinfo=timezone.utc))
        finally:
            zoneinfo.ZoneInfo.clear_cache(); zoneinfo.reset_tzpath(old)
