"""Telegram menu acceptance: scope/ownership, durable edits and usable native forms."""
import asyncio
import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram import Chat, Message, User, Document, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TimedOut, BadRequest
from telegram.ext import ApplicationHandlerStop
from cognition.scope import CURRENT_SCOPE, TransportScope
from materials.types import AccessContext, MaterialScope, MaterialError
from bot.menu.store import MenuStore
from bot.menu.panel import Panel
from bot.menu.controller import Controller
from bot.menu import forms, views, public_action
from tests.materials import test_agents as _agents


def update_for(user=7, chat=55, topic=-1, text='/menu', message_id=1, group=False, reply=None):
    u=User(user,'Тест',False)
    c=Chat(chat,'supergroup' if group else 'private',title='Группа' if group else None,is_forum=group and topic>0)
    m=Message(message_id,datetime.now(timezone.utc),c,from_user=u,text=text,message_thread_id=topic if topic>0 else None,reply_to_message=reply)
    return NS(message=m,effective_message=m,effective_user=u,effective_chat=c,callback_query=None,update_id=message_id)


def context_for():
    bot=NS(send_message=AsyncMock(return_value=NS(message_id=80,chat=NS(id=55))),
        edit_message_text=AsyncMock(return_value=NS(message_id=80,chat=NS(id=55))),
        edit_message_reply_markup=AsyncMock(return_value=True),
        send_document=AsyncMock(return_value=NS(message_id=81,chat=NS(id=55))),
        get_me=AsyncMock(return_value=NS(id=99)), id=99,username='arti')
    return NS(bot=bot,user_data={},chat_data={},bot_data={},application=NS())


class MenuPureTests(unittest.TestCase):
    def test_all_major_capabilities_have_named_entry_points(self):
        buttons=[label for _,rows in views.SECTIONS.values() for line in rows for label,_ in line]
        for fragment in ('Картинку','Видео','Музыку','Инфографику','Задачи','Память','Учебный сценарий','Выбрать модель','Мои голоса','Посчитать'):
            self.assertTrue(any(fragment in label for label in buttons),fragment)
        self.assertFalse(any('/' in label for label in buttons))

    def test_typed_values_do_not_interpret_injected_commands(self):
        with self.assertRaises(ValueError): forms.parse(forms.field('cell','Ячейка',kind='cell'),'B2; /stop')
        with self.assertRaises(ValueError): forms.parse(forms.field('n','Число',kind='number'),'NaN')
        with self.assertRaises(ValueError): forms.parse(forms.field('due','Дата',kind='datetime'),'2026-10-05T18:00')
        self.assertEqual('B2:B12',forms.parse(forms.RANGE,'b2:b12'))
        self.assertEqual('2026-10-05T18:00:00+05:00',forms.parse(forms.field('due','Дата',kind='datetime'),'2026-10-05 18:00 +05:00'))

    def test_shared_navigation_never_grants_private_action_access(self):
        scope=TransportScope(-55,4,'supergroup',8)
        row=dict(chat_id=-55,topic_id=4,state=dict(shared=True))
        self.assertTrue(public_action(row,dict(nav='create'),scope,8))
        for action in (dict(legacy_text='1:1'),dict(effect_approve='t',id='x'),dict(value='yes'),dict(submit=True)):
            self.assertFalse(public_action(row,action,scope,8))
        self.assertFalse(public_action(row,dict(nav='create'),replace(scope,topic_id=5),8))

    def test_upload_snapshot_drops_nested_conversation_and_serializes_date(self):
        original=update_for(text='secret nested conversation').message
        m=Message(3,datetime.now(timezone.utc),original.chat,from_user=original.from_user,
            document=Document('file','unique',file_name='Таблица.csv',file_size=12),reply_to_message=original)
        value=forms.parse(forms.FILE,'',m)
        self.assertNotIn('reply_to_message',value)
        self.assertEqual('Таблица.csv',value['document']['file_name'])
        self.assertIsNotNone(Message.de_json(value,None).document)

    def test_nested_bindings_have_typed_fields_without_json_input(self):
        schema=dict(type='object',properties=dict(settings=dict(type='object',properties=dict(count=dict(type='integer'),enabled=dict(type='boolean')),required=['count','enabled']),
                    values=dict(type='array',items=dict(type='number'))),required=['settings','values'])
        fields=forms.binding_fields(schema)
        draft=dict(fields=fields,values={})
        for f in fields:
            value='2' if f['binding_path'][-1]=='count' else 'false' if f['binding_path'][-1]=='enabled' else '1.5\n2.5'
            draft['values'][f['key']]=forms.parse(f,value)
        self.assertEqual(dict(settings=dict(count=2,enabled=False),values=[1.5,2.5]),forms.bindings(draft))
        summary=''.join(forms.summary_pages(draft))
        self.assertIn('1.5\n2.5',summary)
        self.assertIn('Нет',summary)

    def test_confirmation_keeps_all_values_without_broken_html(self):
        from html import unescape
        fields=[forms.field('goal','Задание'),forms.field('count','Количество',kind='integer')]
        goal='<&>"\''*1000
        draft=dict(fields=fields,values=dict(goal=goal,count=0))
        pages=forms.summary_pages(draft)
        self.assertGreater(len(pages),1)
        self.assertTrue(all(len(page)<3600 for page in pages))
        # Unescape each page separately: no entity may span two messages.
        actual=''.join(unescape(page.split('\n\n',1)[1]) for page in pages)
        self.assertEqual('Задание: '+goal+'\n\nКоличество: 0',actual)

    def test_long_html_never_leaves_partial_tags_or_entities(self):
        from bot.menu.panel import bounded_html
        text=bounded_html('<b>'+('&lt;&amp;'*1000)+'</b>')
        self.assertNotIn('<b>',text); self.assertLess(len(text),3600)
        self.assertFalse(text.rstrip('…\n').endswith('&'))

    def test_background_results_detach_from_navigation_surface(self):
        from bot.queue import _detach_menu_context, _real_menu_context
        actual=NS(bot=object())
        task=dict(context=NS(bot='menu surface',_menu_real_context=actual))
        _detach_menu_context(task)
        self.assertIs(actual,task['context'])
        surface=NS(_menu_panel=object(),real_bot=actual.bot,controller=NS(context=actual))
        legacy=NS(bot=surface)
        task=dict(context=legacy,bot=surface)
        _detach_menu_context(task)
        self.assertIs(actual,task['context']); self.assertIs(actual.bot,task['bot'])
        self.assertIs(actual,_real_menu_context(legacy))


class MenuLegacyACLTests(unittest.IsolatedAsyncioTestCase):
    def callback(self,data,message_id=80):
        update=update_for(user=8,chat=-55,topic=0,group=True)
        message=Message(message_id,datetime.now(timezone.utc),update.effective_chat,from_user=User(99,'Арти',True))
        query=NS(message=message,from_user=update.effective_user,data=data,answer=AsyncMock(),edit_message_text=AsyncMock())
        update.callback_query=query; update.effective_message=message
        return update

    async def test_foreign_photo_and_document_buttons_preserve_both_users_pending_inputs(self):
        from bot import handlers
        import config
        token=CURRENT_SCOPE.set(None)
        try:
            for callback,pending,prefix in ((handlers.photo_action_callback,config.pending_photo_action,'photo_act'),
                                            (handlers.document_action_callback,config.pending_doc_action,'doc_act')):
                with self.subTest(prefix=prefix), patch.dict(pending,{},clear=True):
                    pending[(-55,7)]=dict(bot_message_id=80,secret='owner')
                    own=dict(bot_message_id=81,images=[],message_id=10,replied_to_bot=False,is_private=False)
                    pending[(-55,8)]=own
                    foreign=self.callback(prefix+':cancel')
                    await callback(foreign,context_for())
                    foreign.callback_query.edit_message_text.assert_not_awaited()
                    self.assertIs(own,pending[(-55,8)])
                    self.assertEqual('owner',pending[(-55,7)]['secret'])
                    valid=self.callback(prefix+':cancel',81)
                    await callback(valid,context_for())
                    valid.callback_query.edit_message_text.assert_awaited_once()
                    self.assertNotIn((-55,8),pending)
                    self.assertEqual('owner',pending[(-55,7)]['secret'])
        finally:
            CURRENT_SCOPE.reset(token)

    async def test_foreign_voice_and_model_buttons_cannot_use_own_pending_flow(self):
        from bot import commands
        import config
        token=CURRENT_SCOPE.set(None)
        try:
            for callback,states,data in ((commands.vclone_clean_callback,config.vclone_flow_state,'vclone_clean:0'),
                                          (commands.vclone_save_callback,config.vclone_save_flow_state,'vsave:no')):
                with self.subTest(data=data), patch.dict(states,{},clear=True):
                    state=dict(step='cleanup_choice',bot_message_id=81,reference_path='do-not-touch.wav')
                    states[-55][8]=state
                    update=self.callback(data)
                    await callback(update,context_for())
                    self.assertIs(state,states[-55][8])
                    update.callback_query.edit_message_text.assert_not_awaited()
            context=context_for(); context.user_data['model_flow']=dict(menu_message_id=81,page=0)
            with patch('bot.commands.is_admin',AsyncMock(return_value=True)):
                update=self.callback('model_next')
                await commands.model_callback(update,context)
            self.assertEqual(0,context.user_data['model_flow']['page'])
            self.assertEqual(81,context.user_data['model_flow']['menu_message_id'])
        finally:
            CURRENT_SCOPE.reset(token)

    async def test_group_roleplay_button_cannot_close_group_scene(self):
        import config
        actor=AccessContext(MaterialScope('arti',-55,0,'supergroup'),8,'user:8')
        controller=Controller(NS(row={}),update_for(user=8,chat=-55,topic=0,group=True),context_for(),actor)
        with patch.dict(config.rp_mode_state,{-55:True}),patch('cognition.runtime.get_runtime',return_value=NS(new_scene=AsyncMock())) as runtime:
            with self.assertRaises(MaterialError):
                await controller.act(dict(special='rp_off'))
            self.assertTrue(config.rp_mode_state[-55])
            runtime.assert_not_called()


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class MenuSQLTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=_agents.AgentSQLTests.asyncSetUp
    asyncTearDown=_agents.AgentSQLTests.asyncTearDown

    async def test_long_confirmation_requires_review_and_survives_restart(self):
        store,panel,actor=await self.setup_panel()
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            c=Controller(panel,self.update,self.context,actor)
            await c.begin('project_new',{})
            await c.answer('Длинная цель'); await c.answer('<&>'*1200)
            self.assertFalse(any(a.get('submit') for a in panel.row['actions'].values()))
            with self.assertRaises(MaterialError):
                await c.submit()
            restored=Panel(store,await store.get(55,-1,7),self.context.bot)
            c=Controller(restored,self.update,self.context,actor)
            while True:
                next_action=next((a for a in restored.row['actions'].values() if a.get('confirm_page',-1)>restored.row['state']['confirm_page']),None)
                if next_action is None:
                    break
                await c.act(next_action)
            self.assertTrue(any(a.get('submit') for a in restored.row['actions'].values()))
            self.assertEqual('<&>'*1200,restored.row['state']['draft']['values']['goal'])
        self.context.bot.send_message.assert_awaited_once()

    async def setup_panel(self, *, group=False):
        flags=patch.dict(os.environ,dict(ARTI_MATERIALS_ENABLED='1',ARTI_AGENTS_ENABLED='1'))
        flags.start(); self.addCleanup(flags.stop)
        enabled=patch('materials.runtime.enabled',return_value=True)
        enabled.start(); self.addCleanup(enabled.stop)
        actor=AccessContext(MaterialScope('arti',-55 if group else 55,4 if group else -1,'supergroup' if group else 'private'),7,'user:7')
        scope=TransportScope(actor.scope.chat_id,actor.scope.topic_id,actor.scope.chat_type,7)
        self.scope_token=CURRENT_SCOPE.set(scope)
        self.addCleanup(CURRENT_SCOPE.reset,self.scope_token)
        self.context=context_for()
        self.update=update_for(chat=scope.chat_id,topic=scope.topic_id,group=group)
        store=MenuStore(self.pool)
        row=await store.open(scope.chat_id,scope.topic_id,7,actor.scope.key)
        panel=Panel(store,row,self.context.bot)
        await panel.render('Меню',[[('Создать',dict(nav='create'))]],allow_create=True,state=dict(shared=True))
        return store,panel,actor

    async def test_navigation_edits_single_panel_across_reopen_and_restart(self):
        store,panel,actor=await self.setup_panel()
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            c=Controller(panel,self.update,self.context,actor)
            await c.show('home'); await c.show('create'); await c.show('home')
            restored=Panel(store,await store.get(55,-1,7),self.context.bot)
            await Controller(restored,self.update,self.context,actor).show('settings')
        self.context.bot.send_message.assert_awaited_once()
        self.assertTrue(all(call.kwargs['message_id']==80 for call in self.context.bot.edit_message_text.await_args_list))
        for call in self.context.bot.edit_message_text.await_args_list:
            for line in call.kwargs['reply_markup'].inline_keyboard:
                for b in line: self.assertLessEqual(len(b.callback_data.encode()),64)

    async def test_identical_screen_does_not_repeat_network_edit(self):
        store,panel,actor=await self.setup_panel()
        await panel.render('Тот же экран',[[('Назад',dict(nav='home'))]])
        count=self.context.bot.edit_message_text.await_count
        await panel.render('Тот же экран',[[('Назад',dict(nav='home'))]])
        self.assertEqual(count,self.context.bot.edit_message_text.await_count)

    async def test_timeout_never_creates_replacement_or_retries_send(self):
        store,panel,actor=await self.setup_panel()
        self.context.bot.edit_message_text.side_effect=TimedOut()
        with self.assertRaises(TimedOut): await panel.render('Другой экран')
        row=await store.get(55,-1,7)
        self.assertEqual('unknown',row['status']); self.assertEqual(80,row['message_id'])
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.edit_message_text.assert_awaited_once()

    async def test_initial_send_unknown_retains_intent_without_auto_retry(self):
        store,panel,actor=await self.setup_panel()
        await store.save(panel.row,message_id=None,status='new')
        self.context.bot.send_message.reset_mock(); self.context.bot.send_message.side_effect=TimedOut()
        with self.assertRaises(TimedOut): await panel.render('Открыть',allow_create=True)
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual('unknown',(await store.get(55,-1,7))['status'])

    async def test_wrong_owner_topic_mode_and_old_button_are_rejected(self):
        store,panel,actor=await self.setup_panel(group=True)
        token=next(iter(panel.row['actions']))
        args=dict(user_id=7,chat_id=-55,topic_id=4,message_id=80,scope_key=actor.scope.key)
        for field,value in [('user_id',8),('topic_id',5),('scope_key','rp'),('message_id',81)]:
            with self.assertRaises(MaterialError): store.action(panel.row,token,**dict(args,**{field:value}))
        await panel.render('Новое меню',[[('Создать',dict(nav='create'))]])
        with self.assertRaises(MaterialError): store.action(panel.row,token,**args)

    async def test_native_update_is_consumed_once_under_concurrent_requests(self):
        store,panel,_=await self.setup_panel()
        values=await asyncio.gather(*[store.consume(panel.row,'native-update') for _ in range(8)])
        self.assertEqual(1,sum(values))

    async def test_many_users_do_not_exhaust_pool_with_session_locks(self):
        store,panel,actor=await self.setup_panel()
        from bot.menu.store import _LOCK_GATES
        async def open_user(user):
            async with store.locked(55,-1,user):
                await asyncio.sleep(0.02)
                row=await store.open(55,-1,user,actor.scope.key)
                await store.save(row,state=dict(user=user))
                return row['user_id']
        count=self.pool.get_max_size()*3
        results=await asyncio.wait_for(asyncio.gather(*(open_user(user) for user in range(100,100+count))),10)
        self.assertEqual(count,len(set(results)))
        self.assertNotIn(id(self.pool),_LOCK_GATES)

    async def test_common_menu_forks_for_another_user_without_editing_owner_panel(self):
        from bot.menu import menu_callback
        store,panel,actor=await self.setup_panel(group=True)
        token=next(iter(panel.row['actions']))
        update=update_for(user=8,chat=-55,topic=4,group=True)
        update.effective_message=update.message=Message(80,datetime.now(timezone.utc),update.effective_chat,from_user=User(99,'Арти',True))
        update.callback_query=NS(data='menu:'+panel.row['id']+':'+token,message=update.message,id='other-open',answer=AsyncMock(),from_user=update.effective_user)
        other=AccessContext(actor.scope,8,'user:8')
        CURRENT_SCOPE.set(TransportScope(-55,4,'supergroup',8))
        with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('materials.runtime.actor_for_current',AsyncMock(return_value=other)):
            await menu_callback(update,self.context)
        original=await store.get(-55,4,7); own=await store.get(-55,4,8)
        self.assertEqual(panel.row['revision'],original['revision'])
        self.assertEqual(8,own['user_id']); self.assertEqual('create',own['screen'])
        # Telegram redelivery of the shared click must not clear the private state.
        await store.save(own,state=dict(pending='form',draft=dict(secret='retained user input')))
        with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('materials.runtime.actor_for_current',AsyncMock(return_value=other)):
            await menu_callback(update,self.context)
        self.assertEqual('retained user input',(await store.get(-55,4,8))['state']['draft']['secret'])

    async def test_other_user_cannot_click_generation_settings_even_with_group_access(self):
        from bot.menu import menu_callback
        store,panel,actor=await self.setup_panel(group=True)
        await panel.render('Выбери размер',[[('1:1',dict(legacy_text='1:1'))]],state=dict(pending='legacy'))
        token=next(iter(panel.row['actions']))
        update=update_for(user=8,chat=-55,topic=4,group=True)
        update.effective_message=update.message=Message(80,datetime.now(timezone.utc),update.effective_chat,from_user=User(99,'Арти',True))
        update.callback_query=NS(data='menu:'+panel.row['id']+':'+token,message=update.message,id='foreign-generate',answer=AsyncMock(),from_user=update.effective_user)
        CURRENT_SCOPE.set(TransportScope(-55,4,'supergroup',8))
        with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('materials.runtime.actor_for_current',AsyncMock(return_value=AccessContext(actor.scope,8,'user:8'))),patch('bot.menu.bridge.command',AsyncMock()) as execute:
            await menu_callback(update,self.context)
        execute.assert_not_awaited()
        self.assertIsNone(await store.get(-55,4,8))
        self.assertEqual(panel.row['revision'],(await store.get(-55,4,7))['revision'])
        self.assertTrue(update.callback_query.answer.await_args.kwargs['show_alert'])

    async def test_group_input_requires_owner_reply_to_exact_panel(self):
        from bot.menu import is_menu_input
        store,panel,actor=await self.setup_panel(group=True)
        until=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
        await store.save(panel.row,state=dict(pending='form',input_until=until))
        ordinary=update_for(chat=-55,topic=4,group=True,text='обычный разговор')
        with patch('bot.menu.pool_for_menu',return_value=self.pool):
            self.assertFalse(await is_menu_input(ordinary))
            correct=update_for(chat=-55,topic=4,group=True,text='ответ',reply=Message(80,datetime.now(timezone.utc),ordinary.effective_chat))
            self.assertTrue(await is_menu_input(correct))
            foreign=update_for(user=8,chat=-55,topic=4,group=True,text='ответ',reply=correct.message.reply_to_message)
            self.assertFalse(await is_menu_input(foreign))

    async def test_form_keeps_values_after_restore_and_requires_final_confirmation(self):
        store,panel,actor=await self.setup_panel()
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            c=Controller(panel,self.update,self.context,actor)
            await c.begin('project_new',{})
            await c.answer('Исследование')
            restored=await store.get(55,-1,7)
            self.assertEqual('Исследование',restored['state']['draft']['values']['title'])
            c=Controller(Panel(store,restored,self.context.bot),self.update,self.context,actor)
            with patch('bot.menu.bridge.command',AsyncMock()) as write:
                await c.answer('Сравнить варианты')
                write.assert_not_awaited()
                self.assertEqual('confirm_form',c.panel.row['state']['pending'])

    async def test_cancel_form_clears_retained_inputs_and_does_not_cancel_chat_jobs(self):
        store,panel,actor=await self.setup_panel()
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('bot.queue.cancel_chat_generation') as cancel:
            c=Controller(panel,self.update,self.context,actor)
            await c.begin('project_new',{}); await c.answer('Личные сведения'); await c.show('home')
        self.assertNotIn('draft',(await store.get(55,-1,7))['state']); cancel.assert_not_called()

    async def test_model_browsing_does_not_capture_regular_chat_text(self):
        from bot.menu import is_menu_input
        store,panel,actor=await self.setup_panel()
        await store.save(panel.row,state=dict(pending='legacy',expects_text=False,input_until=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()))
        with patch('bot.menu.pool_for_menu',return_value=self.pool):
            self.assertFalse(await is_menu_input(update_for(text='Привет')))

    async def test_text_bridge_edits_panel_but_file_result_is_separate(self):
        from io import BytesIO
        from bot.menu.bridge import MessageSurface
        store,panel,actor=await self.setup_panel()
        c=Controller(panel,self.update,self.context,actor)
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            m=MessageSurface(c,text='/dataset')
            await m.reply_text('Готово')
            await m.reply_document(BytesIO(b'result'),caption='Результат')
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.send_document.assert_awaited_once()
        self.assertEqual(80,panel.row['message_id'])

    async def test_legacy_reply_keyboard_becomes_owned_inline_settings(self):
        from telegram import ReplyKeyboardMarkup
        store,panel,actor=await self.setup_panel()
        c=Controller(panel,self.update,self.context,actor)
        await store.save(panel.row,state=dict(pending='legacy',legacy_command='image'))
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            await c.capture('Выбери размер',ReplyKeyboardMarkup([['1:1','16:9']]))
        self.assertFalse(panel.row['state'].get('shared',False))
        self.assertTrue(any(v.get('legacy_text')=='1:1' for v in panel.row['actions'].values()))

    async def test_source_erasure_filters_artifact_from_current_project_list(self):
        from artifacts.revisions import ArtifactRepository
        from materials.lifecycle import MaterialLifecycle
        repo=ArtifactRepository(self.materials)
        artifact=await repo.create(self.p.id,self.actor,dict(contract='artifact-1',title='Визуальный результат',format='cards',
            elements=[dict(id='x',label='Предложение',text='Текст',status='proposed')],relations=[],style={}),sources=self.refs)
        store,panel,actor=await self.setup_panel()
        c=Controller(panel,self.update,self.context,actor); c.service=self.service
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('materials.runtime.enabled',return_value=True):
            await c.object_list('artifacts')
            self.assertTrue(any(v.get('id')==artifact['id'] for v in panel.row['actions'].values()))
            await MaterialLifecycle(self.materials,self.service.store).forget(self.asset['id'],self.actor)
            await c.object_list('artifacts')
            self.assertFalse(any(v.get('id')==artifact['id'] for v in panel.row['actions'].values()))

    async def test_project_creation_uses_native_confirm_event_and_human_result(self):
        store,panel,actor=await self.setup_panel()
        self.update.callback_query=NS(id='create-project',from_user=self.update.effective_user)
        c=Controller(panel,self.update,self.context,actor); c.service=self.service
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('materials.runtime.service_for_bot',AsyncMock(return_value=self.service)),patch('materials.runtime.enabled',return_value=True):
            await c.begin('project_new',{}); await c.answer('Новый проект'); await c.answer('Проверить идеи'); await c.submit()
        p=await self.projects.current(actor)
        self.assertEqual('Новый проект',p.title)
        self.assertEqual('project',panel.row['screen'])
        self.context.bot.send_message.assert_awaited_once()

    async def test_callback_source_is_human_action_not_bot_panel_message(self):
        from bot.work_cards import request_sources
        from bot.menu.bridge import MessageSurface
        store,panel,actor=await self.setup_panel()
        self.update.callback_query=NS(id='actual-human-click')
        c=Controller(panel,self.update,self.context,actor)
        refs=await request_sources(self.service,actor,MessageSurface(c,text='Подтвердить выбор'))
        async with self.pool.acquire() as conn:
            source=await conn.fetchval('SELECT source_id FROM material_assets WHERE id=$1',refs[0].asset_id)
        self.assertIn('menu-callback:actual-human-click',source)
        self.assertNotIn(':80:user',source)

    async def test_explicit_group_menu_opens_own_fresh_panel_without_editing_other_user(self):
        from bot.menu import menu_command
        store,panel,actor=await self.setup_panel(group=True)
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            await Controller(panel,self.update,self.context,actor).show('home')
        other=AccessContext(actor.scope,8,'user:8')
        CURRENT_SCOPE.set(TransportScope(-55,4,'supergroup',8))
        update=update_for(user=8,chat=-55,topic=4,group=True,message_id=9)
        before=await store.get(-55,4,7)
        self.context.bot.send_message.return_value=NS(message_id=91,chat=NS(id=-55))
        with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('materials.runtime.actor_for_current',AsyncMock(return_value=other)):
            await menu_command(update,self.context)
        self.assertEqual(2,self.context.bot.send_message.await_count)
        self.assertEqual(91,(await store.get(-55,4,8))['message_id'])
        self.assertEqual(before['revision'],(await store.get(-55,4,7))['revision'])
        self.context.bot.edit_message_reply_markup.assert_not_awaited()

    async def test_explicit_reopen_is_new_message_but_redelivery_and_navigation_are_not(self):
        from bot.menu import menu_command
        store,panel,actor=await self.setup_panel()
        old_token=next(iter(panel.row['actions']))
        self.context.bot.send_message.reset_mock()
        self.context.bot.edit_message_text.reset_mock()
        self.context.bot.send_message.side_effect=[NS(message_id=90,chat=NS(id=55)),NS(message_id=100,chat=NS(id=55))]
        with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            update=update_for(message_id=101)
            await menu_command(update,self.context)
            current=await store.get(55,-1,7)
            self.assertEqual(90,current['message_id'])
            self.context.bot.edit_message_text.assert_not_awaited()
            self.context.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=55,message_id=80,reply_markup=None)
            with self.assertRaises(MaterialError):
                store.action(current,old_token,user_id=7,chat_id=55,topic_id=-1,message_id=80,scope_key=actor.scope.key)
            await menu_command(update,self.context)
            self.context.bot.send_message.assert_awaited_once()
            await Controller(Panel(store,current,self.context.bot),update,self.context,actor).show('create')
            self.assertEqual(90,self.context.bot.edit_message_text.await_args.kwargs['message_id'])
            await menu_command(update_for(message_id=102),self.context)
        self.assertEqual(2,self.context.bot.send_message.await_count)
        self.assertEqual(100,(await store.get(55,-1,7))['message_id'])
        self.assertEqual(90,self.context.bot.edit_message_reply_markup.await_args.kwargs['message_id'])

    async def test_open_menu_abandons_native_image_flow_without_affecting_other_user(self):
        from bot.menu import menu_command
        import config
        store,panel,actor=await self.setup_panel()
        self.context.user_data['image_flow']=dict(chat_id=55,step='aspect_ratio')
        with patch.dict(config.waiting_for_image_prompt,{},clear=True):
            config.waiting_for_image_prompt[55][7]=True
            config.waiting_for_image_prompt[55][8]=True
            with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
                await menu_command(update_for(message_id=101),self.context)
            self.assertNotIn('image_flow',self.context.user_data)
            self.assertNotIn(7,config.waiting_for_image_prompt[55])
            self.assertTrue(config.waiting_for_image_prompt[55][8])
            self.assertNotIn('pending',(await store.get(55,-1,7))['state'])

    async def test_expired_session_reopen_redelivery_still_sends_once(self):
        from bot.menu import menu_command
        store,panel,actor=await self.setup_panel()
        await self.pool.execute("UPDATE arti_menu_sessions SET expires_at=NOW()-INTERVAL '3 days' WHERE id=$1",panel.row['id'])
        self.context.bot.send_message.reset_mock()
        self.context.bot.send_message.return_value=NS(message_id=90,chat=NS(id=55))
        with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            update=update_for(message_id=101)
            await menu_command(update,self.context)
            await menu_command(update,self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(90,(await store.get(55,-1,7))['message_id'])

    async def test_old_menu_cleanup_failure_does_not_block_fresh_open(self):
        from bot.menu import menu_command
        store,panel,actor=await self.setup_panel()
        self.context.bot.send_message.return_value=NS(message_id=90,chat=NS(id=55))
        self.context.bot.edit_message_reply_markup.side_effect=TimedOut()
        with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)):
            await menu_command(update_for(message_id=101),self.context)
        row=await store.get(55,-1,7)
        self.assertEqual(90,row['message_id']); self.assertEqual('active',row['status'])
        self.assertEqual(2,self.context.bot.send_message.await_count)
        self.context.bot.edit_message_reply_markup.assert_awaited_once()

    async def test_cancel_discards_durable_legacy_snapshot_so_it_cannot_restore_input(self):
        from bot.commands import handle_cancel_command
        from bot.menu import is_menu_input
        store,panel,actor=await self.setup_panel()
        until=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
        await store.save(panel.row,state=dict(pending='legacy',input_until=until,expects_text=True,
            legacy_snapshot=dict(config=dict(waiting_for_image_prompt=True))))
        update=update_for(text='/cancel',message_id=101)
        update.message=update.effective_message=NS(from_user=update.effective_user,message_id=101,reply_text=AsyncMock())
        with patch('bot.menu.pool_for_menu',return_value=self.pool),patch('bot.queue.cancel_chat_generation'):
            await handle_cancel_command(update,self.context)
            self.assertFalse(await is_menu_input(update_for(text='обычный разговор',message_id=102)))
        row=await store.get(55,-1,7)
        self.assertEqual({},row['state']); self.assertEqual({},row['actions'])

    async def test_native_input_router_stops_second_handler_and_does_not_observe_llm(self):
        from bot.menu import menu_input
        store,panel,actor=await self.setup_panel()
        c=Controller(panel,self.update,self.context,actor)
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('bot.menu.pool_for_menu',return_value=self.pool),patch('bot.handlers.handle_all_messages',AsyncMock()) as normal:
            await c.begin('project_new',{})
            with self.assertRaises(ApplicationHandlerStop):
                await menu_input(update_for(text='Имя проекта',message_id=7),self.context)
            normal.assert_not_awaited()
        row=await store.get(55,-1,7)
        self.assertEqual('Имя проекта',row['state']['draft']['values']['title'])

    async def test_forged_confirmation_still_rechecks_current_project_rights(self):
        store,panel,actor=await self.setup_panel()
        c=Controller(panel,self.update,self.context,actor); c.service=self.service
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_project_members SET role='viewer' WHERE project_id=$1 AND user_id=7",self.p.id)
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('materials.runtime.enabled',return_value=True):
            with self.assertRaises(MaterialError):
                await c.act(dict(project_control='delete',id=self.p.id,revision=self.p.revision))
        self.assertEqual('active',(await self.projects.get(self.p.id,actor)).status)

    async def test_artifact_text_form_applies_supported_patch_and_preserves_source_guard(self):
        from artifacts.revisions import ArtifactRepository
        store,panel,actor=await self.setup_panel()
        artifact=await ArtifactRepository(self.materials).create(self.p.id,actor,dict(contract='artifact-1',title='Карточка',format='cards',
            elements=[dict(id='block',label='Предложение',text='Старый текст',status='proposed')],relations=[],style={}),sources=self.refs)
        c=Controller(panel,self.update,self.context,actor); c.service=self.service
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('materials.runtime.enabled',return_value=True),patch('bot.work_cards.request_sources',AsyncMock(return_value=self.refs)):
            await c.begin('artifact_replace',dict(id=artifact['id'],revision=1,element='block'))
            await c.answer('Новый текст'); await c.submit()
        fresh=await ArtifactRepository(self.materials).get(artifact['id'],actor)
        self.assertEqual('Новый текст',fresh['spec']['elements'][0]['text']); self.assertEqual(2,fresh['revision'])

    async def test_two_workflow_actions_in_one_panel_have_distinct_native_receipts(self):
        # Native handlers import runtime helpers into their own module. Patch
        # those aliases too, independent of which earlier test loaded them.
        import bot.workflow_commands
        store,panel,actor=await self.setup_panel()
        c=Controller(panel,self.update,self.context,actor); c.service=self.service
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('materials.runtime.service_for_bot',AsyncMock(return_value=self.service)),patch('materials.runtime.enabled',return_value=True),\
             patch('bot.workflow_commands.actor_for_current',AsyncMock(return_value=actor)),patch('bot.workflow_commands.service_for_bot',AsyncMock(return_value=self.service)),patch('bot.workflow_commands.enabled',return_value=True):
            for n in (1,2):
                self.update.callback_query=NS(id='decision-'+str(n),from_user=self.update.effective_user)
                await c.begin('decision_new',{}); await c.answer('Выбор '+str(n)); await c.answer('Первый\nВторой'); await c.submit()
        async with self.pool.acquire() as conn:
            count=await conn.fetchval("SELECT COUNT(*) FROM arti_work_delivery WHERE delivery_key LIKE 'workflow-command:%' AND status='delivered'")
        self.assertEqual(2,count)
        self.assertNotIn('/decision propose',self.context.bot.edit_message_text.await_args.kwargs['text'])

    async def test_external_preview_issues_no_grant_and_role_change_blocks_approval(self):
        from agents.tasks import TaskRepository
        from agents.tools.core import build_registry
        store,panel,actor=await self.setup_panel()
        repo=TaskRepository(self.materials,build_registry())
        plan=dict(goal='Создать запись',steps=[dict(id='write',tool='connector.calendar.write',version='1',
             args=dict(collection='safe',operation='create',payload=dict(title='Встреча')),depends=[])],checks=[dict(step='write',path=['result'],op='nonempty')])
        row=await repo.create(actor,self.p.id,plan,self.refs)
        c=Controller(panel,self.update,self.context,actor); c.service=self.service
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('materials.runtime.enabled',return_value=True):
            await c.act(dict(effect_preview=row['id'],step='write'))
            approval=next(a for a in panel.row['actions'].values() if 'effect_approve' in a)
            async with self.pool.acquire() as conn:
                self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM arti_capability_grants'))
                await conn.execute("UPDATE arti_project_members SET role='viewer' WHERE project_id=$1 AND user_id=7",self.p.id)
            with self.assertRaises(MaterialError): await c.act(approval)
        async with self.pool.acquire() as conn:
            self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM arti_capability_grants'))

    async def test_download_and_format_navigation_keep_owned_result_controls(self):
        from artifacts.revisions import ArtifactRepository
        store,panel,actor=await self.setup_panel()
        artifact=await ArtifactRepository(self.materials).create(self.p.id,actor,dict(contract='artifact-1',title='Результат',format='cards',
            elements=[dict(id='block',label='Предложение',text='Текст',status='proposed')],relations=[],style={}),sources=self.refs)
        self.update.callback_query=NS(id='result-click',answer=AsyncMock(),from_user=self.update.effective_user)
        c=Controller(panel,self.update,self.context,actor); c.service=self.service
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('materials.runtime.service_for_bot',AsyncMock(return_value=self.service)):
            await c.object('artifacts',artifact['id'])
            actions=list(panel.row['actions'].values())
            async with self.pool.acquire() as conn:
                source=None
                for a in actions:
                    if 'legacy' in a and await conn.fetchval('SELECT action FROM arti_work_actions WHERE id=$1',a['legacy'][5:])=='sources':
                        source=a; break
                self.assertIsNotNone(source)
            await c.act(source)
            self.context.bot.send_document.assert_awaited_once()
            self.assertEqual('artifact',panel.row['screen'])
            async with self.pool.acquire() as conn:
                fmt=None
                for a in panel.row['actions'].values():
                    if 'legacy' in a and await conn.fetchval('SELECT action FROM arti_work_actions WHERE id=$1',a['legacy'][5:])=='format':
                        fmt=a; break
                self.assertIsNotNone(fmt)
            await c.act(fmt)
            self.assertEqual('legacy',panel.row['screen'])
            labels=[b.text for line in self.context.bot.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard for b in line]
            self.assertIn('Сравнение',labels); self.assertIn('Хронология',labels)

    async def test_procedure_and_subscription_wizards_use_verified_task_without_json(self):
        from agents.executor import Executor
        from agents.procedures import ProcedureRepository
        from agents.subscriptions import SubscriptionRepository
        store,panel,actor=await self.setup_panel()
        task=await self.repo.create(actor,self.p.id,_agents.plan(),self.refs)
        done=await Executor(self.repo,self.service).run(task['id']); self.assertEqual('succeeded',done['status'])
        done=await self.repo.get(task['id'],actor)
        self.update.callback_query=NS(id='save-recipe',from_user=self.update.effective_user)
        c=Controller(panel,self.update,self.context,actor); c.service=self.service
        with patch('materials.runtime.actor_for_current',AsyncMock(return_value=actor)),patch('materials.runtime.service_for_bot',AsyncMock(return_value=self.service)),patch('agents.tools.core.build_registry',return_value=self.registry):
            await c.begin('procedure_save',dict(id=task['id'],revision=done['revision']))
            await c.answer('Проверенный способ'); await c.submit()
            action=next(a for a in panel.row['actions'].values() if a.get('form')=='subscription_new')
            procedure=await ProcedureRepository(self.materials,self.registry).get(action['id'],actor)
            self.assertEqual('Проверенный способ',procedure['body']['title'])
            self.update.callback_query.id='schedule-recipe'
            await c.act(action)
            for value in ('Asia/Yekaterinburg','weekdays','18:00','10','20','1'):
                await c.answer(value)
            await c.submit()
            self.assertEqual('subscription_detail',panel.row['screen'])
            id=panel.row['state']['view']['id']
            row=await SubscriptionRepository(self.materials,self.registry).get(id,actor)
            self.assertEqual([0,1,2,3,4],row['body']['schedule']['weekdays'])
            self.assertEqual(10,row['body']['max_runs'])

    async def test_reference_loss_after_restart_cannot_silently_change_generation(self):
        store,panel,actor=await self.setup_panel()
        c=Controller(panel,self.update,self.context,actor)
        await store.save(panel.row,state=dict(pending='legacy',legacy_snapshot=dict(requires_reupload=True)))
        with self.assertRaises(MaterialError) as error:
            c.restore_legacy()
        self.assertEqual('menu_reference_reupload',error.exception.code)
