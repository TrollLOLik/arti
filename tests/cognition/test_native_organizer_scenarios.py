"""Native mutations: disposable SQL, fake transport, no provider/network access."""
import asyncio
import os
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from organizer.repository import Repository
from organizer.time import OrganizerError
from bot.organizer_commands import handle_natural
from cognition.scope import TransportScope


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class NativeOrganizerScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        await ensure_schema(self.pool)
        self.repo=Repository(self.pool); self.mid=0; self.sent=1000
        async def send(**kwargs):
            self.sent+=1
            return NS(message_id=self.sent)
        self.bot=NS(send_message=AsyncMock(side_effect=send))

    async def asyncTearDown(self): await self.db.__aexit__(None,None,None)

    async def say(self,text,*,mid=None,reply=None,owner=1,chat=None,raw=None):
        self.mid+=1; mid=mid or self.mid; chat=owner if chat is None else chat
        request=dict(chat_id=chat,user_id=owner,message_id=mid,user_message=text,
            _telegram_scope=TransportScope(chat,-1,'private' if chat>0 else 'supergroup',owner,mid,reply_to_id=reply))
        if raw is not None: request['_native_user_message']=raw
        with patch('bot.organizer_commands.repository',return_value=self.repo):
            handled=await handle_natural(request,self.bot)
        return self.bot.send_message.await_args.kwargs['text'] if handled else None

    async def todo(self,title='Купить чай',key='fixture'):
        return await self.repo.create(1,1,'todo',title,key)

    async def reminder(self,title='Позвонить',key='fixture'):
        return await self.repo.create(1,1,'reminder',title,key,due_at=datetime.now(timezone.utc)+timedelta(hours=1),timezone_name='UTC')

    async def test_native_rename_complete_cancel_and_replay_do_not_revert(self):
        item=await self.todo()
        await self.say(f'Переименуй задачу {item["id"]} в Купить кофе',mid=10)
        self.assertEqual((await self.repo.get(1,1,item['id']))['title'],'Купить кофе')
        await self.say(f'Переименуй задачу {item["id"]} в Купить молоко',mid=11)
        await self.say(f'Переименуй задачу {item["id"]} в Купить кофе',mid=10)
        self.assertEqual((await self.repo.get(1,1,item['id']))['title'],'Купить молоко')
        await self.say(f'Заверши задачу {item["id"]}',mid=12)
        self.assertEqual((await self.repo.get(1,1,item['id']))['state'],'done')
        await self.say(f'Отмени задачу {item["id"]}',mid=13)
        self.assertEqual((await self.repo.get(1,1,item['id']))['state'],'cancelled')

    async def test_reschedule_collects_clock_and_zone_durably(self):
        item=await self.reminder()
        self.assertIn('Во сколько',await self.say(f'Перенеси напоминание {item["id"]} на завтра'))
        self.repo=Repository(self.pool)
        self.assertIn('часовом поясе',await self.say('09:00'))
        self.assertEqual((await self.repo.get(1,1,item['id']))['version'],1)
        await self.say('Москва')
        current=await self.repo.get(1,1,item['id'])
        self.assertEqual(current['version'],2); self.assertEqual(current['timezone'],'Europe/Moscow')

    async def test_missing_date_never_defaults_to_today(self):
        self.assertIn('какую дату',await self.say('Напомни в 09:00 позвонить'))
        self.assertEqual(await self.repo.list(1,1),[])
        self.assertIn('часовом поясе',await self.say('завтра'))
        await self.say('UTC')
        self.assertEqual(len(await self.repo.list(1,1)),1)

    async def test_date_without_clock_never_defaults_to_midnight(self):
        day=(datetime.now(timezone.utc)+timedelta(days=2)).date().isoformat()
        self.assertIn('Во сколько',await self.say(f'Напомни {day} позвонить'))
        self.assertIn('часовом поясе',await self.say('10:00'))
        await self.say('UTC')
        self.assertEqual((await self.repo.list(1,1))[0]['due_at'].hour,10)

    async def test_duplicate_title_requires_exact_owned_id(self):
        first=await self.todo('Покупки','first'); second=await self.todo('Покупки','second')
        response=await self.say('Заверши задачу Покупки')
        self.assertIn(first['id'],response); self.assertIn(second['id'],response)
        self.assertIn('точный ID',await self.say('первую'))
        await self.say(second['id'])
        self.assertEqual((await self.repo.get(1,1,first['id']))['state'],'active')
        self.assertEqual((await self.repo.get(1,1,second['id']))['state'],'done')

    async def test_reply_reference_is_verified_and_versioned(self):
        await self.say('Добавь задачу Купить чай')
        receipt=self.sent; item=(await self.repo.list(1,1))[0]
        await self.say('untrusted quoted material',raw='Переименуй это в Купить кофе',reply=receipt)
        self.assertEqual((await self.repo.get(1,1,item['id']))['title'],'Купить кофе')
        response=await self.say('Заверши это',reply=receipt)
        self.assertIn('уже изменилась',response)
        self.assertEqual((await self.repo.get(1,1,item['id']))['state'],'active')

    async def test_unverified_reply_demands_id_and_does_not_infer_latest(self):
        item=await self.todo()
        response=await self.say('Заверши это',reply=555)
        self.assertIn('точный ID',response)
        await self.say(item['id'])
        self.assertEqual((await self.repo.get(1,1,item['id']))['state'],'done')

    async def test_reply_to_multi_item_list_requires_target_selection(self):
        first=await self.todo('Первое','first'); second=await self.todo('Второе','second')
        await self.say('Покажи мои задачи'); receipt=self.sent
        response=await self.say('Отмени это',reply=receipt)
        self.assertIn('несколько',response)
        self.assertEqual(len(await self.repo.list(1,1)),2)
        await self.say(first['id'])
        self.assertEqual((await self.repo.list(1,1))[0]['id'],second['id'])

    async def test_stale_clarification_cannot_edit_newer_item_version(self):
        item=await self.reminder()
        await self.say(f'Перенеси напоминание {item["id"]} на завтра')
        await self.repo.edit(1,1,item['id'],title='Новое',source_key='other')
        await self.say('09:00')
        response=await self.say('UTC')
        self.assertEqual((await self.repo.get(1,1,item['id']))['version'],2)
        self.assertNotIn('Сохранено',response or '')

    async def test_two_concurrent_versioned_edits_have_one_winner(self):
        item=await self.todo()
        results=await asyncio.gather(*(self.repo.edit(1,1,item['id'],title=f'New {i}',source_key=f'edit{i}',expected_version=1) for i in range(2)),return_exceptions=True)
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertEqual(sum(isinstance(result,OrganizerError) for result in results),1)

    async def test_reschedule_invalidates_old_claim_and_preserves_single_delivery(self):
        from organizer.runtime import dispatch_once
        item=await self.reminder()
        async with self.pool.acquire() as conn: await conn.execute("UPDATE arti_organizer_items SET due_at=NOW()-INTERVAL '1 second' WHERE id=$1",item['id'])
        claimed=await self.repo.claim_due()
        await self.say(f'Перенеси напоминание {item["id"]} на через 30 минут')
        self.assertIsNone(await self.repo.begin_send(item['id'],claimed['token']))
        sender=AsyncMock(return_value=NS(message_id=99))
        self.assertFalse(await dispatch_once(None,repo=self.repo,sender=sender))
        sender.assert_not_awaited()

    async def test_unknown_delivery_cannot_be_rescheduled_or_reopened(self):
        item=await self.reminder()
        async with self.pool.acquire() as conn: await conn.execute("UPDATE arti_organizer_items SET due_at=NOW()-INTERVAL '1 second' WHERE id=$1",item['id'])
        claimed=await self.repo.claim_due(); await self.repo.begin_send(item['id'],claimed['token'])
        await self.repo.finish_send(item['id'],claimed['token'])
        self.assertIn('результат неизвестен',await self.say(f'Перенеси напоминание {item["id"]} на через 5 минут'))
        self.assertIn('только личным задачам',await self.say(f'Заверши напоминание {item["id"]}'))
        self.assertEqual((await self.repo.get(1,1,item['id']))['notification_state'],'delivery_unknown')
        self.assertIsNone(await self.repo.claim_due())

    async def test_todo_move_and_recurrence_never_create_hidden_reminder(self):
        item=await self.todo()
        self.assertIn('отдельное напоминание',await self.say(f'Перенеси задачу {item["id"]} на завтра'))
        self.assertIn('не создано',await self.say(f'Перенеси напоминание {item["id"]} на каждый день'))
        self.assertEqual(await self.repo.list(1,1,'reminder'),[])

    async def test_cancel_pending_blocks_replayed_root_and_stale_commit(self):
        from bot.queue import cancel_chat_generation
        await self.say('Добавь задачу',mid=20)
        await cancel_chat_generation(1)
        self.assertIn('отменено',await self.say('Добавь задачу',mid=20))
        self.assertIsNone(await self.say('Купить хлеб',mid=21))
        with self.assertRaisesRegex(OrganizerError,'request_cancelled'):
            await self.repo.create(1,1,'todo','Stale result','telegram:1:20')
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_source_tombstone_before_ingestion_blocks_creation(self):
        from materials.lifecycle import forget_sources
        await forget_sources(self.pool,__import__('materials.types',fromlist=['context_identity']).context_identity('arti',1,-1,'default',''),1,['telegram:1:25:user'])
        self.assertIn('удалён',await self.say('Добавь задачу Купить чай',mid=25))
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_source_erasure_scrubs_item_pending_list_cache_and_replay(self):
        from materials.lifecycle import forget_sources
        await self.say('Напомни через час Секретное название',mid=30)
        item=(await self.repo.list(1,1))[0]
        await self.say('Покажи мои напоминания',mid=31)
        await self.say(f'Перенеси напоминание {item["id"]} на завтра',mid=32)
        await forget_sources(self.pool,__import__('materials.types',fromlist=['context_identity']).context_identity('arti',1,-1,'default',''),1,['telegram:1:30:user'])
        self.assertEqual(await self.repo.list(1,1,include_closed=True),[])
        self.assertIsNone(await self.say('09:00',mid=33))
        for mid,text in ((30,'Напомни через час Секретное название'),(31,'Покажи мои напоминания'),(32,f'Перенеси напоминание {item["id"]} на завтра')):
            response=await self.say(text,mid=mid)
            self.assertNotIn('Секретное',response)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM arti_organizer_turns WHERE outcome::text LIKE '%Секретное%'"),0)

    async def test_group_foreign_and_forged_sender_do_not_reveal_or_mutate(self):
        item=await self.todo('Личное')
        response=await self.say(f'Заверши задачу {item["id"]}',owner=2)
        self.assertNotIn('Личное',response)
        with patch('bot.organizer_commands.repository',side_effect=AssertionError('private storage accessed')):
            request=dict(chat_id=-10,user_id=1,message_id=100,user_message=f'Заверши задачу {item["id"]}',
                _telegram_scope=TransportScope(-10,4,'supergroup',1,100))
            self.assertTrue(await handle_natural(request,self.bot))
        self.assertEqual((await self.repo.get(1,1,item['id']))['state'],'active')

    async def test_quotes_negations_and_other_speakers_do_not_mutate(self):
        item=await self.todo()
        for text in (f'Не заверши задачу {item["id"]}',f'Мама просит: заверши задачу {item["id"]}',f'«Заверши задачу {item["id"]}»',f'Переведи: заверши задачу {item["id"]}'):
            self.assertIsNone(await self.say(text))
        self.assertEqual((await self.repo.get(1,1,item['id']))['state'],'active')

    async def test_expired_root_replay_does_not_resurrect_clarification(self):
        await self.say('Добавь задачу',mid=70)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_organizer_pending SET expires_at=NOW()-INTERVAL '1 second'")
        self.assertIn('отменено',await self.say('Добавь задачу',mid=70))
        self.assertIsNone(await self.say('Купить чай',mid=71))
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_reset_between_send_and_receipt_record_cannot_restore_reference(self):
        from organizer.scenarios import cancel_pending
        item=await self.todo()
        async with self.pool.acquire() as conn,conn.transaction():
            await cancel_pending(conn,1)
        await self.repo.record_reply(1,1,555,[dict(id=item['id'],version=1)],generation=0)
        self.assertEqual(await self.repo.resolve(1,1,'это',reply_to_id=555),[])
        self.assertEqual((await self.repo.get(1,1,item['id']))['state'],'active')

    async def test_invalid_calendar_date_stays_a_validation_response(self):
        await self.say('Напомни 2026-99-99 позвонить')
        await self.say('09:00')
        response=await self.say('UTC')
        self.assertIn('дата и время',response)
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_upgrade_scrubs_reminder_forgotten_before_migration(self):
        from materials.types import context_identity
        from cognition.repositories import ensure_schema
        item=await self.reminder(key='telegram:1:90')
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('ALTER TABLE material_source_tombstones DISABLE TRIGGER arti_organizer_source_tombstoned')
            await conn.execute('INSERT INTO material_source_tombstones(scope_key,owner_id,source_id) VALUES($1,1,$2)',context_identity('arti',1,-1,'default',''),'telegram:1:90:user')
            await conn.execute('ALTER TABLE material_source_tombstones ENABLE TRIGGER arti_organizer_source_tombstoned')
            await conn.execute("DELETE FROM cognitive_schema_migrations WHERE name='052_native_organizer_scenarios.sql'")
        self.assertIsNotNone(await self.repo.get(1,1,item['id']))
        await ensure_schema(self.pool)
        self.assertIsNone(await self.repo.get(1,1,item['id']))
        self.assertIsNone(await self.repo.claim_due())

    async def test_explicit_material_work_prefix_is_not_organizer_completion(self):
        from organizer.natural import parse,claim_input
        await self.say('Добавь задачу',mid=100)
        for text in ('Выполни задачу: сравни два файла','Выполни задачу, сделай схему','Агент: составь отчёт'):
            self.assertIsNone(parse(text))
            self.assertFalse(await claim_input(self.pool,1,1,101,text))
            self.assertIsNone(await self.say(text))
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_upgrade_scrubs_only_unlinked_native_checkpoints(self):
        from bot.request_store import RequestStore
        from cognition.repositories import ensure_schema
        requests=RequestStore(self.pool)
        legacy=await requests.enqueue('text',1,-1,'legacy-native',{})
        other=await requests.enqueue('text',2,-1,'ordinary-work',{})
        reminder=await self.reminder(key='unaffected-reminder')
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute("UPDATE arti_requests SET checkpoints='{\"organizer_result\":{\"response\":\"LegacySecret\"}}'::jsonb WHERE id=$1",legacy['id'])
            await conn.execute("UPDATE arti_requests SET checkpoints='{\"ordinary_result\":{\"response\":\"KeepMe\"}}'::jsonb WHERE id=$1",other['id'])
            await conn.execute("INSERT INTO arti_request_sends(request_id,ordinal,payload) VALUES($1,1,'{\"text\":\"LegacySecret\"}')",legacy['id'])
            await conn.execute("DELETE FROM cognitive_schema_migrations WHERE name='052_native_organizer_scenarios.sql'")
        await ensure_schema(self.pool)
        self.assertEqual((await requests.status(legacy['id']))['state'],'cancelled')
        self.assertEqual((await requests.status(other['id']))['state'],'queued')
        self.assertEqual((await self.repo.get(1,1,reminder['id']))['notification_state'],'pending')
        async with self.pool.acquire() as conn:
            self.assertNotIn('LegacySecret',str(await conn.fetch('SELECT checkpoints,payload FROM arti_requests WHERE id=$1',legacy['id'])))
            self.assertNotIn('LegacySecret',str(await conn.fetch('SELECT payload FROM arti_request_sends WHERE request_id=$1',legacy['id'])))
            self.assertIn('KeepMe',str(await conn.fetchval('SELECT checkpoints FROM arti_requests WHERE id=$1',other['id'])))
