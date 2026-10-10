"""Real menu/command integration, with disposable SQL and no media providers."""
import ast
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import Message, User, Video, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop
from cognition.scope import CURRENT_SCOPE, TransportScope
from materials.types import AccessContext, MaterialScope
from bot.menu import media_command, menu_callback, menu_input
from bot.menu.store import MenuStore
from tests.materials.test_menu import context_for, update_for


class MediaMenuContractTests(unittest.TestCase):
    def test_bot_never_constructs_input_keyboards(self):
        for path in Path('bot').rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
                    self.assertNotIn(name, ('ReplyKeyboardMarkup', 'KeyboardButton'), str(path))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL')
class MediaMenuSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        from bot import commands
        import config
        self.commands = commands
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.addAsyncCleanup(self.db.__aexit__, None, None, None)
        await ensure_schema(self.pool)
        self.scope = TransportScope(-55, 4, 'supergroup', 7)
        token = CURRENT_SCOPE.set(self.scope)
        self.addCleanup(CURRENT_SCOPE.reset, token)
        self.context = context_for()
        self.context.args = []
        self.store = MenuStore(self.pool)
        self.serial = 100
        p = patch('bot.menu.pool_for_menu', return_value=self.pool)
        p.start(); self.addCleanup(p.stop)
        p = patch('materials.runtime.actor_for_current', AsyncMock(side_effect=self.current_actor))
        p.start(); self.addCleanup(p.stop)
        for name, value in (
            ('is_responses_enabled', True), ('handle_spam_protection', True),
            ('_gate_media_quota', False), ('is_admin', True), ('_extract_photo_urls', []),
            ('save_chat_message', None), ('enqueue_generation', None), ('_enqueue_dub_task', None),
        ):
            p = patch('bot.commands.' + name, AsyncMock(return_value=value))
            p.start(); self.addCleanup(p.stop)
        p = patch('bot.handlers._is_text_rate_limited', return_value=False)
        p.start(); self.addCleanup(p.stop)
        p = patch('bot.handlers.is_responses_enabled', AsyncMock(return_value=True))
        p.start(); self.addCleanup(p.stop)
        p = patch('bot.commands.TTS_ENABLED', True)
        p.start(); self.addCleanup(p.stop)
        for name in ('rp_mode_state', 'music_flow_state', 'dub_flow_state',
                     'waiting_for_image_prompt', 'waiting_for_video_prompt',
                     'pending_image_inputs', 'pending_video_inputs', 'pending_photo_action',
                     'vclone_flow_state', 'vclone_save_flow_state'):
            p = patch.dict(getattr(config, name), {}, clear=True)
            p.start(); self.addCleanup(p.stop)

    def current_actor(self):
        scope = CURRENT_SCOPE.get()
        return AccessContext(MaterialScope.from_transport(scope), scope.user_id, f'user:{scope.user_id}')

    async def row(self):
        return await self.store.get(self.scope.chat_id, self.scope.topic_id, 7)

    def update(self, *, text='', user=7, topic=4, message_id=None, reply=None):
        self.serial += 1
        return update_for(user=user, chat=-55, topic=topic, group=True, text=text,
                          message_id=message_id or self.serial, reply=reply)

    async def open(self, kind, args=()):
        self.context.args = list(args)
        update = self.update(text='/' + kind)
        await getattr(self.commands, 'handle_' + kind + '_command')(update, self.context)
        return await self.row()

    async def button(self, label):
        row = await self.row()
        for action_token in row['actions']:
            for method in (self.context.bot.edit_message_text, self.context.bot.send_message):
                for sent in reversed(method.await_args_list):
                    markup = sent.kwargs.get('reply_markup')
                    if isinstance(markup, InlineKeyboardMarkup):
                        for line in markup.inline_keyboard:
                            for button in line:
                                data = 'menu:' + row['id'] + ':' + action_token
                                if button.text == label and button.callback_data == data:
                                    return data
        self.fail(f'No current button {label!r}; screen={row["screen"]}, state={row["state"]}')

    async def click(self, label=None, *, data=None, user=7, topic=4, message_id=80, query_id=None):
        data = data or await self.button(label)
        update = self.update(user=user, topic=topic)
        message = Message(message_id, datetime.now(timezone.utc), update.effective_chat,
                          from_user=User(99, 'Арти', True), message_thread_id=topic)
        update.message = update.effective_message = message
        query = NS(data=data, message=message, id=query_id or str(self.serial),
                   from_user=update.effective_user, answer=AsyncMock())
        update.callback_query = query
        token = CURRENT_SCOPE.set(TransportScope(-55, topic, 'supergroup', user))
        try:
            await menu_callback(update, self.context)
        finally:
            CURRENT_SCOPE.reset(token)
        return query

    async def answer(self, text='', *, video=None, reply=True):
        panel_message = Message(80, datetime.now(timezone.utc), self.update().effective_chat,
                                from_user=User(99, 'Арти', True), message_thread_id=4)
        update = self.update(text=text or None, reply=panel_message if reply else None)
        if video is not None:
            update.message = update.effective_message = Message(update.message.message_id,
                datetime.now(timezone.utc), update.effective_chat, from_user=update.effective_user,
                video=video, message_thread_id=4, reply_to_message=panel_message)
        with self.assertRaises(ApplicationHandlerStop):
            await menu_input(update, self.context)

    def assert_cancel_available(self, row):
        self.assertTrue(any(a.get('nav') == 'home' for a in row['actions'].values()))
        self.assertTrue(any(a.get('nav') in ('create', 'voice_tools') for a in row['actions'].values()))

    async def test_image_and_video_steps_edit_one_owned_message(self):
        for kind, labels, expected in (
            ('image', ['16:9', '2K', '3'], dict(aspect_ratio='16:9', resolution='2K', num_images=3)),
            ('video', ['Veo 3 Lite', '12', '9:16'], dict(model='google/veo-3-lite', duration='12', aspect_ratio='9:16')),
        ):
            with self.subTest(kind=kind):
                sent_before = self.context.bot.send_message.await_count
                await self.open(kind)
                for label in labels:
                    self.assert_cancel_available(await self.row())
                    await self.click(label)
                row = await self.row()
                self.assertTrue(row['state']['expects_text'])
                flow = self.context.user_data[kind + '_flow']
                for key, value in expected.items():
                    self.assertEqual(value, flow[key])
                self.assertEqual(sent_before + 1, self.context.bot.send_message.await_count)
                self.assertTrue(all(c.kwargs['message_id'] == 80 for c in self.context.bot.edit_message_text.await_args_list))
                self.assert_cancel_available(row)
                await self.click('Отменить ввод')

    async def test_foreign_user_wrong_topic_message_and_old_generation_are_rejected(self):
        await self.open('image')
        data = await self.button('16:9')
        before = await self.row()
        for kwargs in (dict(user=8), dict(topic=5), dict(message_id=81)):
            with self.subTest(**kwargs):
                query = await self.click(data=data, **kwargs)
                self.assertTrue(query.answer.await_args.kwargs['show_alert'])
                self.assertEqual(before, await self.row())
        await self.click(data=data)
        revision = (await self.row())['revision']
        await self.click(data=data)
        self.assertEqual(revision, (await self.row())['revision'])
        self.assertEqual('resolution', self.context.user_data['image_flow']['step'])
        self.commands.enqueue_generation.assert_not_awaited()

    async def test_cancel_each_stage_only_clears_owners_input_and_revokes_buttons(self):
        import config
        other = dict(step='source', url='https://example.com/other')
        config.dub_flow_state[-55][8] = other
        for kind, labels in (
            ('image', ['16:9', '2K', '3']), ('video', ['Sora 2', '8', '16:9']),
            ('music', ['🎤 Со словами']), ('dub', []),
        ):
            for count in range(len(labels) + 1):
                with self.subTest(kind=kind, count=count):
                    await self.open(kind)
                    for label in labels[:count]:
                        await self.click(label)
                    old = await self.button('Отменить ввод')
                    await self.click(data=old)
                    row = await self.row()
                    self.assertNotIn('pending', row['state'])
                    self.assertNotIn(kind + '_flow', self.context.user_data)
                    self.assertFalse(config.music_flow_state.get(-55, {}).get(7))
                    self.assertFalse(config.dub_flow_state.get(-55, {}).get(7))
                    self.assertIs(other, config.dub_flow_state[-55][8])
                    revision = row['revision']
                    await self.click(data=old)
                    self.assertEqual(revision, (await self.row())['revision'])
        self.commands.enqueue_generation.assert_not_awaited()
        self.commands._enqueue_dub_task.assert_not_awaited()

    async def test_music_format_and_text_steps_keep_cancel_and_actual_settings(self):
        import config
        await self.open('music')
        self.assertFalse((await self.row())['state']['expects_text'])
        await self.click('🎸 Инструментал (без слов)')
        self.assertTrue(config.music_flow_state[-55][7]['instrumental'])
        await self.answer('Synthwave')
        self.assert_cancel_available(await self.row())
        await self.answer('скип')
        task = self.commands.enqueue_generation.await_args.args[0]
        self.assertEqual((task['style'], task['instrumental'], task['prompt']), ('Synthwave', True, ''))
        self.assertNotIn('pending', (await self.row())['state'])

    async def test_subtitle_choices_are_exact_and_duplicate_click_cannot_enqueue_twice(self):
        for label, expected in (('❌ Без субтитров', False), ('✅ Да, с субтитрами', True)):
            with self.subTest(label=label):
                await self.open('dub')
                await self.answer('https://example.com/video.mp4')
                self.assert_cancel_available(await self.row())
                data = await self.button(label)
                before = self.commands._enqueue_dub_task.await_count
                await asyncio.gather(self.click(data=data, query_id='same-click-' + str(self.serial)),
                                     self.click(data=data, query_id='same-click-' + str(self.serial)))
                self.assertEqual(before + 1, self.commands._enqueue_dub_task.await_count)
                self.assertIs(expected, self.commands._enqueue_dub_task.await_args.kwargs['with_subs'])
                await self.click(data=data)
                self.assertEqual(before + 1, self.commands._enqueue_dub_task.await_count)

    async def test_dub_rechecks_permissions_at_submission(self):
        await self.open('dub')
        await self.answer('https://example.com/video.mp4')
        self.commands.is_admin.return_value = False
        await self.click('✅ Да, с субтитрами')
        self.commands._enqueue_dub_task.assert_not_awaited()
        self.assert_cancel_available(await self.row())
        await self.click('Отменить ввод')

    async def test_cancel_video_attachment_deletes_only_owners_temporary_file(self):
        import config
        root = Path('temp').resolve()
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='media_menu_test_', dir=root) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(root))
            other = Path(directory) / 'other.mp4'
            other.write_bytes(b'other')
            config.dub_flow_state[-55][8] = dict(step='subs', input_file=str(other))
            await self.open('dub')
            async def download(*, custom_path):
                path = Path(custom_path).resolve()
                self.assertTrue(path.is_relative_to(root))
                path.write_bytes(b'video')
                return path
            file = NS(file_size=5, download_to_drive=AsyncMock(side_effect=download))
            self.context.bot.get_file = AsyncMock(return_value=file)
            await self.answer(video=Video('file', 'unique', 16, 9, 1, file_name='sample.mp4'))
            own = Path(config.dub_flow_state[-55][7]['input_file']).resolve()
            self.addCleanup(own.unlink, missing_ok=True)
            await self.click('Отменить ввод')
            self.assertFalse(own.exists())
            self.assertTrue(other.exists())
            self.commands._enqueue_dub_task.assert_not_awaited()

    async def test_video_url_action_adopts_its_message_without_extra_panel(self):
        update = self.update()
        update.callback_query = NS(id='url-dub', message=Message(80, datetime.now(timezone.utc),
            update.effective_chat, from_user=User(99, 'Арти', True)), from_user=update.effective_user)
        update.message = update.effective_message = update.callback_query.message
        await media_command(update, self.context, 'dub', source='https://example.com/video.mp4')
        self.context.bot.send_message.assert_not_awaited()
        self.assertEqual(80, (await self.row())['message_id'])
        await self.click('❌ Без субтитров')
        self.assertFalse(self.commands._enqueue_dub_task.await_args.kwargs['with_subs'])

    async def test_direct_prompt_preserves_arguments_and_replied_reference(self):
        self.commands._extract_photo_urls.return_value = ['https://example.com/reference.jpg']
        await self.open('image', ['A', "character's", 'portrait'])
        task = self.commands.enqueue_generation.await_args.args[0]
        self.assertEqual("A character's portrait", task['prompt'])
        self.assertEqual(['https://example.com/reference.jpg'], task['image_urls'])
        self.assertNotIn('pending', (await self.row())['state'])

    async def test_old_input_keyboard_is_removed_selectively_once_then_notice_deleted(self):
        from telegram import ReplyKeyboardRemove
        self.context.user_data.pop('_input_keyboard_removed')
        self.context.bot.send_message.side_effect = [NS(message_id=75), NS(message_id=80)]
        await self.open('image')
        cleanup = self.context.bot.send_message.await_args_list[0]
        self.assertIsInstance(cleanup.kwargs['reply_markup'], ReplyKeyboardRemove)
        self.assertTrue(cleanup.kwargs['reply_markup'].selective)
        self.assertEqual(4, cleanup.kwargs['message_thread_id'])
        self.assertEqual(101, cleanup.kwargs['reply_to_message_id'])
        self.context.bot.delete_message.assert_awaited_once_with(chat_id=-55,message_id=75)
        await self.click('16:9')
        self.assertEqual(2, self.context.bot.send_message.await_count)

    async def test_cancel_music_lyrics_step_does_not_submit_or_touch_other_user(self):
        import config
        other = dict(step='lyrics', style='other')
        config.music_flow_state[-55][8] = other
        await self.open('music')
        await self.click('🎤 Со словами')
        await self.answer('Synthwave')
        await self.click('Отменить ввод')
        self.commands.enqueue_generation.assert_not_awaited()
        self.assertIs(other, config.music_flow_state[-55][8])
        self.assertNotIn('pending', (await self.row())['state'])

    async def test_menu_entry_keeps_one_panel_and_owned_cancel_through_final_prompt(self):
        from bot.menu import menu_command
        await menu_command(self.update(text='/menu'), self.context)
        await self.click('✨ Придумаем')
        await self.click('🎨 Картинку')
        cancel = await self.button('Отменить ввод')
        before = await self.row()
        await self.click(data=cancel, user=8)
        self.assertEqual(before, await self.row())
        for label in ('1:1', '1K', '1'):
            await self.click(label)
        text = self.context.bot.edit_message_text.await_args.kwargs['text']
        self.assertIn('Для отмены нажми «Отменить ввод»', text)
        self.assertNotIn('/cancel', text)
        self.context.bot.send_message.assert_awaited_once()
        await self.click('Отменить ввод')
        self.assertEqual('create', (await self.row())['screen'])
        self.assertNotIn('image_flow', self.context.user_data)
