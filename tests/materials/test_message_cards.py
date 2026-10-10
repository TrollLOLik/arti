"""Offline card semantics, native transport, menu capture and send ambiguity."""
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut
from bot.buttons import inline_button
from bot.cards import MessageCard, task_card, request_card, transcript_card, artifact_card, card_transport, reply_card


class BalancedHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.stack=[]
    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
    def handle_endtag(self, tag):
        assert self.stack.pop()==tag


class MessageCardTests(unittest.TestCase):
    def test_long_unicode_and_hostile_source_keep_cautions_and_valid_markup(self):
        card=MessageCard('<img src="unsafe"> & отчёт', 'Готово', ('<>&😀'*3000,)*20,
            ('<script>source</script>'*1000,), ('Полнота неизвестна.', 'Источник: fixture'),
            ('A','B'), (('x','y'),)*40)
        text=card.html()
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
        self.assertNotIn('<img',text)
        self.assertIn('&lt;img',text)
        self.assertIn('Полнота неизвестна.',text)
        self.assertIn('Источник: fixture',text)
        parser=BalancedHTML(); parser.feed(text); self.assertFalse(parser.stack)
        self.assertIn('[Обзор сокращён',text)
        self.assertIn('<table',card.rich_html())
        self.assertIn('Показаны первые 24 строки',card.rich_html())

    def test_task_progress_is_persisted_steps_not_call_count(self):
        plan=dict(goal='Разобрать встречу',steps=[dict(id='A',tool='audio.transcript'),dict(id='B',tool='artifact.create')])
        row=dict(id='technical-id',revision=2,status='running',used_calls=9,diagnostics=None)
        calls=[dict(step_id='A',attempt=1,status='failed'),dict(step_id='A',attempt=2,status='success'),dict(step_id='B',attempt=1,status='started')]
        text=task_card(row,plan,calls).html()
        self.assertTrue(text.startswith('<b>Разобрать встречу</b>'))
        self.assertIn('Готово этапов: 1/2',text)
        self.assertIn('✅ Расшифровать запись',text)
        self.assertIn('⏳ Собрать отчёт',text)
        self.assertIn('Сохранено: Расшифровка',text)
        self.assertGreater(text.index('technical-id'),text.index('<blockquote'))

    def test_waiting_and_unknown_do_not_claim_completion(self):
        row=dict(id='id',revision=1,status='waiting',used_calls=0)
        plan=dict(goal='Отчёт',steps=[dict(id='A',tool='artifact.create')])
        self.assertIn('○ Собрать отчёт',task_card(row,plan,[]).html())
        row['status']='unknown'
        text=task_card(row,plan,[]).html()
        self.assertIn('могло уже выполниться',text)
        self.assertNotIn('✅ Собрать',text)

    def test_request_unknown_and_paused_explain_next_action(self):
        row=dict(id='a'*32,kind='dubbing',state='delivery_unknown')
        self.assertIn('автоматического повтора не будет',request_card(row).html())
        row['state']='paused'
        self.assertIn('платный API',request_card(row).html())
        row['state']='prepared'
        self.assertIn('Результат подготовлен',request_card(row).html())

    def test_transcript_real_time_and_warning_survive_long_text(self):
        timeline=NS(segments=[NS(id='turn_7',start_ms=1500,end_ms=3000,speaker=None,text='<>&'*3000)],limitations=('uncertain',))
        card=transcript_card('Встреча.ogg','abc123',timeline)
        self.assertIn('1.5–3 с',card.html())
        self.assertIn('метка не подтверждает личность',card.html())
        self.assertIn('/listen turn_7',card.rich_html())
        self.assertIn('version=abc123',card.rich_html())
        self.assertIn('<details>',card.rich_html())

    def test_reports_label_proposals_and_use_authorized_spec(self):
        row=dict(revision=2,spec=dict(title='Расходы',elements=[dict(label='Экономия',text='Требует проверки',status='proposed')]))
        text=artifact_card(row).rich_html(preview='telegram-file-id')
        self.assertIn('Предложение',text)
        self.assertIn('Требует проверки',text)
        self.assertIn('tg://photo?id=preview',text)

    def test_small_caption_bounds_entities_and_closes_tags(self):
        card=MessageCard('😀'*300,'Готово',('<>&'*1000,),('source'*1000,),('warning'*1000,))
        text=card.html(limit=950)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,1024)
        parser=BalancedHTML(); parser.feed(text); self.assertFalse(parser.stack)

    def test_expandable_details_survive_menu_html_sanitizer(self):
        from bot.text_delivery import html_chunks
        text=MessageCard('Отчёт',details=('Происхождение результата',)).html()
        self.assertIn('<blockquote expandable>',html_chunks(text)[0])

    def test_document_analysis_preserves_complete_long_answer(self):
        from bot.cards import document_analysis_html
        from bot.text_delivery import html_chunks
        content='<b>Вывод</b>\n'+'X'*20000
        result=document_analysis_html(dict(filename='<file>&.docx',action='summarize'),content)
        parts=html_chunks(result)
        self.assertIn('&lt;file&gt;&amp;.docx',parts[0])
        self.assertIn('Краткое содержание готово',parts[0])
        self.assertEqual(20000,sum(p.count('X') for p in parts))
        self.assertIn('интерпретация модели',parts[-1])

    def test_media_cards_show_actual_format_and_ready_state(self):
        from bot.cards import media_card
        row=dict(id='a'*32,kind='dubbing',state='queued')
        request=dict(type='dubbing',input_media={'sha256':'fixture'},audio_only=True,with_subs=True)
        text=media_card(row,request).html()
        self.assertIn('В очереди',text)
        self.assertIn('Формат: аудио',text)
        self.assertIn('Субтитры: включены',text)
        ready=media_card(row,request,ready=True).html(limit=950)
        self.assertIn('✅ Готово',ready)
        self.assertNotIn('Начну, когда',ready)


class CardTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot=Bot('123:synthetic')
        self.receipt=dict(message_id=91,date=1,chat=dict(id=55,type='private'))
        self.card=MessageCard('Отчёт',headers=('A','B'),rows=(('1','2'),),details=('Источник fixture',))
        self.kw=dict(chat_id=55,text=self.card.html(),rich_html=self.card.rich_html(),
                     message_thread_id=4,reply_to_message_id=9,
                     reply_markup=InlineKeyboardMarkup([[inline_button('PDF',callback_data='work:opaque')]]))

    async def test_native_rich_preserves_topic_reply_and_buttons(self):
        with patch.object(Bot,'_post',AsyncMock(return_value=self.receipt)) as post:
            result=await card_transport(self.bot)(**self.kw)
        self.assertEqual(91,result.message_id)
        endpoint,data=post.await_args.args
        self.assertEqual('sendRichMessage',endpoint)
        self.assertEqual(4,data['message_thread_id'])
        self.assertEqual(9,data['reply_parameters'].message_id)
        self.assertIn('<table',data['rich_message']['html'])
        self.assertEqual('work:opaque',data['reply_markup'].inline_keyboard[0][0].callback_data)

    async def test_definite_unsupported_method_falls_back_once(self):
        with patch.object(Bot,'_post',AsyncMock(side_effect=[BadRequest('Method not found'),self.receipt])) as post:
            result=await card_transport(self.bot)(**self.kw)
        self.assertEqual(91,result.message_id)
        self.assertEqual(['sendRichMessage','sendMessage'],[c.args[0] for c in post.await_args_list])
        self.assertEqual(self.card.html(),post.await_args.args[1]['text'])

    async def test_network_ambiguity_never_falls_back_or_retries(self):
        with patch.object(Bot,'_post',AsyncMock(side_effect=TimedOut())) as post:
            with self.assertRaises(TimedOut): await card_transport(self.bot)(**self.kw)
        post.assert_awaited_once()

    async def test_unrelated_rejection_is_propagated(self):
        with patch.object(Bot,'_post',AsyncMock(side_effect=BadRequest('Chat not found'))) as post:
            with self.assertRaises(BadRequest): await card_transport(self.bot)(**self.kw)
        post.assert_awaited_once()

    async def test_disabled_rich_uses_html_and_keeps_topic(self):
        with patch.dict('os.environ',ARTI_TELEGRAM_RICH_MESSAGES='false'),patch.object(Bot,'_post',AsyncMock(return_value=self.receipt)) as post:
            await card_transport(self.bot)(**self.kw)
        self.assertEqual('sendMessage',post.await_args.args[0])
        self.assertEqual(4,post.await_args.args[1]['message_thread_id'])

    async def test_menu_captures_card_without_new_rich_message(self):
        message=NS(chat_id=55,reply_text=AsyncMock())
        context=NS(_menu_real_context=NS(bot=self.bot))
        with patch('materials.runtime.guard_current',AsyncMock()),patch.object(Bot,'_post',AsyncMock()) as post:
            await reply_card(message,context,self.card)
        post.assert_not_awaited()
        message.reply_text.assert_awaited_once()
        self.assertEqual('HTML',message.reply_text.await_args.kwargs['parse_mode'])

    async def test_source_revoked_at_transport_fence_sends_nothing(self):
        from materials.types import MaterialError
        message=NS(chat_id=55,message_id=9,message_thread_id=4)
        validator=AsyncMock(side_effect=[None,MaterialError('source_erased')])
        with patch('materials.runtime.guard_current',AsyncMock()),patch.object(Bot,'_post',AsyncMock()) as post:
            with self.assertRaises(MaterialError):
                await reply_card(message,NS(bot=self.bot),self.card,validate=validator)
        post.assert_not_awaited()

    async def test_durable_codec_keeps_rich_payload_and_button_style(self):
        from bot.request_codec import encode_value,decode_value
        value=await decode_value(await encode_value(self.kw))
        self.assertEqual(self.kw['rich_html'],value['rich_html'])
        self.assertEqual('primary',value['reply_markup'].inline_keyboard[0][0].to_dict()['style'])

    async def test_library_serializes_media_and_native_buttons(self):
        import json
        from telegram.request import RequestData
        from telegram.request._requestparameter import RequestParameter
        rich=dict(html=self.card.rich_html(preview='id'),media=[dict(id='preview',media=dict(type='photo',media='file-id'))])
        payload=RequestData(parameters=[RequestParameter.from_input('rich_message',rich),
            RequestParameter.from_input('reply_markup',self.kw['reply_markup'])]).json_parameters
        self.assertEqual('file-id',json.loads(payload['rich_message'])['media'][0]['media']['media'])
        self.assertEqual('primary',json.loads(payload['reply_markup'])['inline_keyboard'][0][0]['style'])

    async def test_document_card_metadata_survives_request_restore(self):
        from bot.request_codec import encode_request,decode_request
        original=dict(type='text',chat_id=55,user_id=7,message_id=9,_request_no_coalesce=True,
                      _result_card=dict(filename='file.docx',action='keypoints'))
        result=await decode_request(await encode_request(original),self.bot)
        self.assertEqual(original['_result_card'],result['_result_card'])
        self.assertTrue(result['_request_no_coalesce'])


class RequestCardControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.id='a'*32
        self.row=dict(id=self.id,kind='text',state='running',chat_id=55,topic_id=-1,owner_id='7')
        self.message=NS(chat_id=55,message_thread_id=None,reply_text=AsyncMock())
        self.update=NS(effective_message=self.message,effective_chat=NS(id=55,type='private'),effective_user=NS(id=7))
        self.context=NS(args=[self.id])
        self.store=NS(status=AsyncMock(return_value=self.row))

    async def test_only_owner_sees_stop_button(self):
        from bot.request_runtime import request_status
        with patch('bot.request_runtime.store',return_value=self.store),patch('materials.runtime.guard_current',AsyncMock()):
            await request_status(self.update,self.context)
            own=self.message.reply_text.await_args.kwargs['reply_markup']
            self.assertIn('request_cancel:'+self.id,[b.callback_data for r in own.inline_keyboard for b in r])
            self.update.effective_user.id=8
            await request_status(self.update,self.context)
        other=self.message.reply_text.await_args.kwargs['reply_markup']
        self.assertNotIn('request_cancel:'+self.id,[b.callback_data for r in other.inline_keyboard for b in r])

    async def test_refresh_edits_same_message(self):
        from bot.request_runtime import request_card_callback
        query=NS(data='request_view:'+self.id,answer=AsyncMock(),edit_message_text=AsyncMock(),message=self.message,from_user=NS(id=7))
        update=NS(callback_query=query,effective_chat=self.update.effective_chat)
        with patch('bot.request_runtime.store',return_value=self.store),patch('materials.runtime.guard_current',AsyncMock()):
            await request_card_callback(update,self.context)
        query.edit_message_text.assert_awaited_once()
        self.message.reply_text.assert_not_awaited()
        self.assertEqual([self.id],self.context.args)

    async def test_foreign_topic_is_not_rendered(self):
        from bot.request_runtime import request_status
        self.row['topic_id']=4
        query=NS(edit_message_text=AsyncMock())
        with patch('bot.request_runtime.store',return_value=self.store):
            await request_status(self.update,self.context,query=query)
        query.edit_message_text.assert_not_awaited()
        self.message.reply_text.assert_awaited_once_with('Запрос не найден в этом чате.')

    async def test_stop_callback_passes_real_clicker_to_owner_control(self):
        from bot.request_runtime import request_card_callback
        query=NS(data='request_cancel:'+self.id,answer=AsyncMock(),message=self.message,from_user=NS(id=8))
        update=NS(callback_query=query,effective_chat=self.update.effective_chat)
        with patch('bot.request_runtime.cancel_own_request',AsyncMock()) as cancel,patch('bot.request_runtime.request_status',AsyncMock()):
            await request_card_callback(update,self.context)
        self.assertEqual(8,cancel.await_args.args[0].effective_user.id)


if __name__=='__main__': unittest.main()
