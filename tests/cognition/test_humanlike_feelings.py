"""H3 «Чувства, которые видно»: the voice of the dominant emotion, a fast first
impression, stickers and reactions, conversational rhythm.

Synthetic data only: no Telegram, provider requests or production database.
"""
import asyncio
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot import human_rhythm as rhythm
from cognition import expression_projection as projection
from cognition.affect import dominant_emotion
from cognition.fast_appraisal import fast_signal, MAX_CHARS
from cognition.types import (ExpressionPlan, EMOTION_VOICE, STAGE_VOICE, ALLOWED_REACTIONS, DOMINANT_THRESHOLD,
                             LIGHT_HUMOUR_THRESHOLD, relationship_stage)


def plan(**kw):
    base = dict(regulation='acknowledge', tone='calm', warmth=.6, directness=.5, playfulness=.1,
                disclosure=.2, sticker_mood=None, tts_style='neutral', cause_ids=(), uncertain_intent=False)
    base.update(kw)
    return ExpressionPlan(**base)


def passing(salt, rate, *, failing=False):
    """A deterministic event id that the projection's sampling lets through (or not)."""
    for n in range(10000):
        event_id = f'event-{n}'
        if (projection._unit(event_id, salt) < rate) != failing:
            return event_id
    raise AssertionError('no event id found')


# A labelled synthetic corpus: clear first-person cues and the traps the fast
# appraisal must abstain on (quotes, negation, sarcasm, third parties, topics).
CORPUS = (
    ('Я не хочу жить', 'crisis'),
    ('иногда думаю, что хочу умереть', 'crisis'),
    ('он сказал: «покончить с собой — выход»', 'crisis'),
    ('Мой кот умер сегодня утром', 'loss'),
    ('у меня бабушка умерла', 'loss'),
    ('вчера не стало моего папы', 'loss'),
    ('пришлось усыпить мою собаку', 'loss'),
    ('Меня уволили', 'setback'),
    ('мы расстались с ним', 'setback'),
    ('мне страшно', 'fear'),
    ('у меня паника, не могу успокоиться', 'fear'),
    ('ты тупая железка', 'insult'),
    ('заткнись уже', 'insult'),
    ('ты бесполезная', 'insult'),
    ('прости, я сорвался', 'apology'),
    ('извини за вчерашнее', 'apology'),
    ('Я сдала экзамен!', 'success'),
    ('меня взяли на работу', 'success'),
    ('получил оффер в Яндекс', 'success'),
    ('ураааа', 'success'),
    ('у меня получилось!', 'success'),
    ('спасибо тебе', 'care'),
    ('ты лучшая', 'care'),
    ('скучала по тебе', 'care'),
    ('ахахаха', 'amusement'),
    ('лол, это гениально', 'amusement'),
    ('😂😂', 'amusement'),
    # Abstentions.
    ('Привет, как дела?', None),
    ('в фильме главный герой умер', None),
    ('умер король, да здравствует король', None),
    ('сколько людей погибли в той войне?', None),
    ('у соседа умер попугай', None),
    ('представь, что ты умерла', None),
    ('я не сдал', None),
    ('мне не страшно', None),
    ('нет, я не боюсь', None),
    ('ты не тупая, просто устала', None),
    ('он написал мне «ты дура»', None),
    ('если бы меня уволили, что бы я делал?', None),
    ('расскажи про развод в римском праве', None),
    ('ну спасибо, удружила', None),
    ('очень смешно', None),
    ('ага, конечно, ты лучшая', None),
    ('не прощу', None),
    ('культура и искусство', None),
    ('спасибо за совет. ' + 'Подробно расскажу, как прошла неделя и что изменилось. ' * 6, None),
)


def corpus_metrics():
    predicted = [(text, expected, getattr(fast_signal(text), 'label', None)) for text, expected in CORPUS]
    fired = [p for p in predicted if p[2] is not None]
    positives = [p for p in predicted if p[1] is not None]
    return dict(
        size=len(CORPUS), positives=len(positives), negatives=len(CORPUS) - len(positives),
        precision=sum(p[1] == p[2] for p in fired) / len(fired),
        recall=sum(p[1] == p[2] for p in positives) / len(positives),
        mistakes=[p for p in predicted if p[1] != p[2]])


class FastAppraisalTests(unittest.TestCase):
    def test_labelled_corpus_is_exact(self):
        metrics = corpus_metrics()
        self.assertEqual(metrics['mistakes'], [])
        self.assertEqual(metrics['precision'], 1.)
        self.assertEqual(metrics['recall'], 1.)

    def test_long_message_abstains_but_crisis_never_does(self):
        tail = ' и ещё много слов про погоду' * 12
        self.assertGreater(len('мне страшно' + tail), MAX_CHARS)
        self.assertIsNone(fast_signal('мне страшно' + tail))
        self.assertEqual(fast_signal('не хочу жить' + tail).label, 'crisis')

    def test_situation_is_display_only_and_conservative(self):
        insult = fast_signal('ты тупая').situation()
        self.assertEqual((insult.kind, insult.social_signal, insult.intention_evidence), ('conflict', 'insult', 'explicit'))
        loss = fast_signal('мой пёс умер').situation()
        self.assertEqual((loss.kind, loss.outcome, loss.intention_evidence), ('loss', 'confirmed', 'unobserved'))
        self.assertEqual(fast_signal('ты тупая').emotion, 'anger')
        self.assertIsNone(fast_signal(''))
        self.assertIsNone(fast_signal(None))


class DominantEmotionTests(unittest.TestCase):
    def episode(self, emotion, intensity, target='person'):
        return NS(emotion=emotion, intensity=intensity, target_id=target)

    def test_strongest_summed_emotion_wins_with_stable_tie_break(self):
        active = [self.episode('joy', .1), self.episode('joy', .1), self.episode('sadness', .15)]
        self.assertEqual(dominant_emotion(active), ('joy', .2))
        self.assertEqual(dominant_emotion([self.episode('joy', .1), self.episode('interest', .1)])[0], 'interest')

    def test_anger_without_target_is_indignation_on_the_persons_side(self):
        self.assertEqual(dominant_emotion([self.episode('anger', .3, target=None)])[0], 'indignation')
        self.assertEqual(dominant_emotion([self.episode('anger', .3)])[0], 'anger')

    def test_faint_state_has_no_dominant(self):
        self.assertEqual(dominant_emotion([self.episode('joy', DOMINANT_THRESHOLD / 2)]), (None, 0.))
        self.assertEqual(dominant_emotion([]), (None, 0.))


class EmotionVoiceTests(unittest.TestCase):
    def test_every_emotion_has_a_russian_manner_line(self):
        self.assertIn('indignation', EMOTION_VOICE)
        self.assertEqual(len(EMOTION_VOICE), 12)
        for line in EMOTION_VOICE.values():
            self.assertRegex(line, '[а-яА-Я]')
            self.assertNotRegex(line, '[A-Za-z]')

    def test_feeling_and_stage_reach_the_instruction(self):
        text = plan(dominant='sadness', dominant_intensity=.2, stage='close').instruction()
        self.assertIn(EMOTION_VOICE['sadness'], text)
        self.assertIn(STAGE_VOICE['close'], text)
        self.assertNotIn(EMOTION_VOICE['sadness'], plan(dominant='sadness', dominant_intensity=DOMINANT_THRESHOLD / 2).instruction())

    def test_anger_is_dry_and_precise_never_cruel(self):
        angry = plan(tone='dry and precise', dominant='anger', dominant_intensity=.3, behaviors=('boundary',))
        text = angry.instruction()
        self.assertIn('точнее и суше', text)
        self.assertIn(EMOTION_VOICE['anger'], text)
        self.assertTrue(angry.humour().startswith('Сухая ирония'))
        self.assertIn('без насмешки', angry.humour())
        # Doubt about intent or another serious marker withholds even dry irony.
        self.assertTrue(plan(dominant='anger', dominant_intensity=.3, behaviors=('boundary',), uncertain_intent=True)
                        .humour().startswith('Без шуток'))
        self.assertTrue(plan(dominant='anger', dominant_intensity=.3, behaviors=('boundary', 'repair'))
                        .humour().startswith('Без шуток'))

    def test_playful_feeling_is_silent_on_a_serious_turn(self):
        serious = plan(dominant='joy', dominant_intensity=.3, uncertain_intent=True)
        self.assertNotIn(EMOTION_VOICE['joy'], serious.instruction())
        self.assertIn(EMOTION_VOICE['joy'], plan(dominant='joy', dominant_intensity=.3).instruction())

    def test_provisional_plan_defers_to_the_text(self):
        self.assertIn('первое впечатление', plan(provisional=True).instruction())
        self.assertNotIn('первое впечатление', plan().instruction())

    def test_relationship_stage_thresholds(self):
        self.assertEqual([relationship_stage(v) for v in (.5, .64, .65, .84, .85, .99)],
                         ['new', 'new', 'familiar', 'familiar', 'close', 'close'])

    def test_plan_rejects_unknown_display_values(self):
        for bad in (dict(dominant='rage'), dict(stage='intimate'), dict(reaction='💩'), dict(provisional=1)):
            with self.assertRaises(ValueError):
                plan(**bad)


class ProjectionTests(unittest.TestCase):
    def test_sticker_needs_regulation_permission_and_is_deterministic(self):
        allowed = plan(sticker_mood='happy', stage='familiar')
        event = passing('sticker', projection.STICKER_RATE['familiar'])
        self.assertIsNone(projection.sticker_mood(plan(stage='familiar'), event_id=event))
        self.assertEqual(projection.sticker_mood(allowed, event_id=event), projection.sticker_mood(allowed, event_id=event))
        self.assertIsNone(projection.sticker_mood(allowed, event_id=passing('sticker', projection.STICKER_RATE['familiar'], failing=True)))

    def test_sticker_rate_depends_on_closeness(self):
        for stage, rate in projection.STICKER_RATE.items():
            shown = sum(projection.sticker_mood(plan(sticker_mood='happy', stage=stage), event_id=f'e{n}') is not None
                        for n in range(4000)) / 4000
            self.assertAlmostEqual(shown, rate, delta=.03)
        self.assertLess(projection.STICKER_RATE['new'], projection.STICKER_RATE['close'])

    def test_sticker_classes_follow_the_feeling(self):
        event = passing('sticker', .35)
        tease = next(f'e{n}' for n in range(10000) if projection._unit(f'e{n}', 'sticker') < .5
                     and projection._unit(f'e{n}', 'tease') < .4)
        no_tease = next(f'e{n}' for n in range(10000) if projection._unit(f'e{n}', 'sticker') < .5
                        and projection._unit(f'e{n}', 'tease') >= .4)
        cases = {
            'love': (plan(sticker_mood='happy', stage='close', warmth=.8), 'care', event),
            'blush': (plan(sticker_mood='happy', stage='familiar', warmth=.8), 'care', event),
            'thinking': (plan(sticker_mood='happy', stage='familiar', dominant='interest', dominant_intensity=.1), None, event),
            'shock': (plan(sticker_mood='happy', stage='familiar', dominant='surprise', dominant_intensity=.1), None, event),
            'teasing': (plan(sticker_mood='happy', stage='familiar', playfulness=LIGHT_HUMOUR_THRESHOLD), None, tease),
            'happy': (plan(sticker_mood='happy', stage='familiar', playfulness=LIGHT_HUMOUR_THRESHOLD), None, no_tease),
        }
        for expected, (p, social, event_id) in cases.items():
            self.assertEqual(projection.sticker_mood(p, social=social, event_id=event_id), expected, expected)
        # A new acquaintance is never teased.
        self.assertEqual(projection.sticker_mood(plan(sticker_mood='happy', stage='new', playfulness=.5),
                                                 event_id=passing('sticker', .35)), 'happy')

    def test_reaction_is_private_occasional_and_never_on_sensitive_turns(self):
        event = passing('reaction', projection.REACTION_RATE)
        warm = plan()
        self.assertEqual(projection.reaction(warm, social='care', event_id=event), '❤')
        self.assertEqual(projection.reaction(warm, label='amusement', event_id=event), '😁')
        self.assertEqual(projection.reaction(warm, kind='success', event_id=event), '🔥')
        self.assertEqual(projection.reaction(plan(dominant='pride', dominant_intensity=.1), event_id=event), '🔥')
        self.assertIsNone(projection.reaction(warm, event_id=event))
        self.assertIsNone(projection.reaction(warm, social='care', private=False, event_id=event))
        self.assertIsNone(projection.reaction(plan(uncertain_intent=True), social='care', event_id=event))
        self.assertIsNone(projection.reaction(plan(sticker_mood='happy'), social='care', event_id=event))
        self.assertIsNone(projection.reaction(warm, social='care',
                                              event_id=passing('reaction', projection.REACTION_RATE, failing=True)))
        self.assertLessEqual({'❤', '😁', '🔥'}, ALLOWED_REACTIONS)
        shown = sum(projection.reaction(warm, social='care', event_id=f'e{n}') is not None for n in range(4000)) / 4000
        self.assertAlmostEqual(shown, projection.REACTION_RATE, delta=.03)


class RhythmTests(unittest.TestCase):
    def test_unfinished_fragments_wait_longer_and_never_past_the_cap(self):
        cases = {'Как дела?': rhythm.QUICK, 'слушай': rhythm.INCOMPLETE, 'тут такое…': rhythm.INCOMPLETE,
                 'я пошла и': rhythm.INCOMPLETE, 'короче,': rhythm.INCOMPLETE, 'привет': rhythm.SHORT,
                 'я сегодня ходила в магазин за продуктами': rhythm.DEFAULT, 'а' * 200: rhythm.QUICK, '': rhythm.DEFAULT}
        for text, expected in cases.items():
            self.assertEqual(rhythm.continuation_wait(text), expected, text)
        self.assertLess(rhythm.QUICK, rhythm.DEFAULT)
        self.assertLess(rhythm.DEFAULT, rhythm.SHORT)
        self.assertLess(rhythm.SHORT, rhythm.INCOMPLETE)
        self.assertLessEqual(rhythm.INCOMPLETE, rhythm.DEBOUNCE_MAX_SECONDS)

    def test_bubbles_only_for_two_or_three_short_balanced_paragraphs(self):
        self.assertEqual(rhythm.split_bubbles('Ого.\n\nРасскажи подробнее?'), ['Ого.', 'Расскажи подробнее?'])
        self.assertEqual(rhythm.split_bubbles('a\n\nb\n\nc'), ['a', 'b', 'c'])
        self.assertEqual(rhythm.split_bubbles('a\n\nb\n\nc\n\nd'), ['a\n\nb\n\nc\n\nd'])
        self.assertEqual(rhythm.split_bubbles('одна мысль'), ['одна мысль'])
        self.assertEqual(rhythm.split_bubbles('<b>a\n\nb</b>'), ['<b>a\n\nb</b>'])
        long = 'x' * (rhythm.MAX_BUBBLE_CHARS + 1)
        self.assertEqual(rhythm.split_bubbles('a\n\n' + long), ['a\n\n' + long])

    def test_only_plain_small_talk_is_conversational(self):
        self.assertTrue(rhythm.conversational('Ого, поздравляю!\n\nКак отметишь?'))
        self.assertFalse(rhythm.conversational('Шаги:\n1. открыть\n2. закрыть'))
        self.assertFalse(rhythm.conversational('- раз\n- два'))
        self.assertFalse(rhythm.conversational('<pre>code</pre>'))
        self.assertFalse(rhythm.conversational('x' * (rhythm.CONVERSATIONAL_LIMIT + 1)))
        self.assertFalse(rhythm.conversational('привет', task=True))
        self.assertFalse(rhythm.conversational('привет', card=True))
        self.assertFalse(rhythm.conversational('привет', markup=True))
        self.assertFalse(rhythm.conversational('  '))

    def test_pacing_fills_only_time_not_already_spent(self):
        bubbles = ['Ого.', 'Расскажи подробнее, как всё прошло?']
        slow = rhythm.plan_pacing('я сдала!', bubbles, elapsed=30)
        self.assertEqual(slow[0], 0.)
        fast = rhythm.plan_pacing('я сдала!', bubbles, elapsed=0)
        self.assertAlmostEqual(fast[0], rhythm.read_seconds('я сдала!') + rhythm.typing_seconds('Ого.'))
        self.assertGreater(fast[1], 0)
        self.assertLessEqual(max(fast[1:]), 2.5)
        self.assertEqual(rhythm.typing_seconds('x' * 1000), 6.)
        self.assertEqual(rhythm.read_seconds('x' * 1000), 2.)
        self.assertEqual(rhythm.plain_length('<b>Ого</b> &amp;'), 5)

    def test_pacing_switch(self):
        with patch.dict(os.environ, {'ARTI_HUMAN_PACING': '0'}):
            self.assertFalse(rhythm.pacing_enabled())
        with patch.dict(os.environ, {'ARTI_HUMAN_PACING': '1'}):
            self.assertTrue(rhythm.pacing_enabled())

    def test_acknowledgement_reaction(self):
        self.assertEqual(rhythm.acknowledgement_reaction('Спасибо!'), '❤')
        self.assertEqual(rhythm.acknowledgement_reaction('спасибо большое)'), '❤')
        self.assertEqual(rhythm.acknowledgement_reaction('ок'), '👍')
        self.assertEqual(rhythm.acknowledgement_reaction('Понятно.'), '👍')
        self.assertEqual(rhythm.acknowledgement_reaction('👍'), '👍')
        self.assertEqual(rhythm.acknowledgement_reaction('❤️'), '❤')
        self.assertIsNone(rhythm.acknowledgement_reaction('спасибо', 'Тебе помочь ещё с чем-нибудь?'))
        self.assertIsNone(rhythm.acknowledgement_reaction('спасибо, а как сделать второй шаг'))
        self.assertIsNone(rhythm.acknowledgement_reaction('понятно, но почему?'))
        self.assertIsNone(rhythm.acknowledgement_reaction('привет'))
        self.assertIsNone(rhythm.acknowledgement_reaction(''))
        for emoji in ('❤', '👍'):
            self.assertIn(emoji, ALLOWED_REACTIONS)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from bot.request_runtime import CURRENT_REQUEST
        self.token = CURRENT_REQUEST.set(None)

    async def asyncTearDown(self):
        from bot.request_runtime import CURRENT_REQUEST
        CURRENT_REQUEST.reset(self.token)

    async def test_plan_codec_roundtrip_keeps_display_fields(self):
        from bot.request_codec import encode_value, decode_value
        original = plan(dominant='indignation', dominant_intensity=.4, stage='familiar', reaction='❤', provisional=True)
        self.assertEqual(await decode_value(await encode_value(original)), original)

    async def test_bubbles_are_separate_messages_with_typing_and_markup_last(self):
        from bot.text_delivery import send_html_reply
        bot = NS(send_message=AsyncMock(side_effect=lambda **kw: NS(message_id=7)), send_chat_action=AsyncMock())
        await send_html_reply(bot, chat_id=1, text='Ого.\n\nКак отметишь?', reply_markup='KB',
                              bubbles=True, pacing=lambda parts: [.01] * len(parts))
        sent = [c.kwargs for c in bot.send_message.await_args_list]
        self.assertEqual([s['text'] for s in sent], ['Ого.', 'Как отметишь?'])
        self.assertEqual([s['reply_markup'] for s in sent], [None, 'KB'])
        self.assertTrue(all(s['reply_to_message_id'] is None for s in sent))
        self.assertGreaterEqual(bot.send_chat_action.await_count, 2)

    async def test_without_bubbles_the_reply_is_unchanged(self):
        from bot.text_delivery import send_html_reply
        bot = NS(send_message=AsyncMock(return_value=NS(message_id=7)), send_chat_action=AsyncMock())
        await send_html_reply(bot, chat_id=1, text='Ого.\n\nКак отметишь?', reply_to_message_id=5)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertEqual(bot.send_message.await_args.kwargs['reply_to_message_id'], 5)
        bot.send_chat_action.assert_not_awaited()

    async def test_typing_status_failure_never_blocks_the_reply(self):
        from bot.text_delivery import send_html_reply
        bot = NS(send_message=AsyncMock(return_value=NS(message_id=7)), send_chat_action=AsyncMock(side_effect=OSError))
        await send_html_reply(bot, chat_id=1, text='Ого.', bubbles=True, pacing=lambda parts: [.01])
        self.assertEqual(bot.send_message.await_count, 1)


class AcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from bot.request_runtime import CURRENT_REQUEST
        from cognition.scope import CURRENT_SCOPE, TransportScope
        self.tokens = [(CURRENT_REQUEST, CURRENT_REQUEST.set(None)),
                       (CURRENT_SCOPE, CURRENT_SCOPE.set(TransportScope(5, -1, 'private', 5, 9)))]

    async def asyncTearDown(self):
        for var, token in reversed(self.tokens):
            var.reset(token)

    async def ack(self, text='Спасибо!', history=(('assistant', 'Готово: список на завтра собран.'), ('user', 'Спасибо!')),
                  mode='default', bot=None, **request):
        import bot.queue as queue
        from cognition.prompting import DialogueTurns
        bot = bot or NS(set_message_reaction=AsyncMock(return_value=True))
        turns = AsyncMock(return_value=DialogueTurns(tuple(history)))
        with patch('utils.chat_history.get_dialogue_turns', turns):
            done = await queue._acknowledge_only(dict(chat_id=5, message_id=9, user_message=text, **request), bot, mode)
        return done, bot

    async def test_thanks_after_a_finished_answer_gets_only_a_reaction(self):
        done, bot = await self.ack()
        self.assertTrue(done)
        bot.set_message_reaction.assert_awaited_once_with(chat_id=5, message_id=9, reaction='❤')
        done, bot = await self.ack('ок', history=(('assistant', 'Напомню в восемь.'), ('user', 'ок')))
        self.assertTrue(done)
        self.assertEqual(bot.set_message_reaction.await_args.kwargs['reaction'], '👍')

    async def test_a_question_from_arti_still_deserves_an_answer(self):
        done, bot = await self.ack(history=(('assistant', 'Тебе так удобнее?'), ('user', 'Спасибо!')))
        self.assertFalse(done)
        bot.set_message_reaction.assert_not_awaited()

    async def test_no_reaction_without_a_previous_answer_or_with_unanswered_messages(self):
        self.assertFalse((await self.ack(history=(('user', 'Спасибо!'),)))[0])
        self.assertFalse((await self.ack(history=(('assistant', 'Готово.'), ('user', 'а ещё вопрос'), ('user', 'Спасибо!'))))[0])

    async def test_only_private_default_text(self):
        from cognition.scope import CURRENT_SCOPE, TransportScope
        self.assertFalse((await self.ack(mode='rp'))[0])
        self.assertFalse((await self.ack(base64_image='x'))[0])
        self.assertFalse((await self.ack('Спасибо, а как добавить второе напоминание?'))[0])
        token = CURRENT_SCOPE.set(TransportScope(-100, -1, 'supergroup', 5, 9))
        try:
            self.assertFalse((await self.ack())[0])
        finally:
            CURRENT_SCOPE.reset(token)

    async def test_rejected_reaction_falls_back_to_a_text_reply(self):
        failing = NS(set_message_reaction=AsyncMock(side_effect=RuntimeError('REACTION_INVALID')))
        self.assertFalse((await self.ack(bot=failing))[0])

    async def test_unknown_delivery_is_never_followed_by_a_duplicate_reply(self):
        from cognition.delivery import DeliveryUnknown, DeliverySuppressed
        self.assertTrue((await self.ack(bot=NS(set_message_reaction=AsyncMock(side_effect=DeliveryUnknown()))))[0])
        with self.assertRaises(DeliverySuppressed):
            await self.ack(bot=NS(set_message_reaction=AsyncMock(side_effect=DeliverySuppressed())))

    async def test_reaction_before_reply_is_best_effort(self):
        import bot.queue as queue
        self.assertFalse(await queue._react(NS(set_message_reaction=AsyncMock(side_effect=RuntimeError)), 5, 9, '❤'))
        ok = NS(set_message_reaction=AsyncMock(return_value=True))
        self.assertTrue(await queue._react(ok, 5, 9, '🔥'))
        ok.set_message_reaction.assert_awaited_once_with(chat_id=5, message_id=9, reaction='🔥')

    async def test_conversational_reply_excludes_tasks_and_groups(self):
        import bot.queue as queue
        from cognition.scope import CURRENT_SCOPE, TransportScope
        text = 'Ого, поздравляю!\n\nКак отметишь?'
        self.assertTrue(queue._conversational_reply({}, 'default', text, None, task=False))
        self.assertFalse(queue._conversational_reply({}, 'rp', text, None, task=False))
        self.assertFalse(queue._conversational_reply({}, 'default', text, None, task=True))
        self.assertFalse(queue._conversational_reply({'_intent': {'web_search': True}}, 'default', text, None, task=False))
        self.assertFalse(queue._conversational_reply({'_result_card': {'x': 1}}, 'default', text, None, task=False))
        self.assertFalse(queue._conversational_reply({}, 'default', text, object(), task=False))
        token = CURRENT_SCOPE.set(TransportScope(-100, -1, 'supergroup', 5, 9))
        try:
            self.assertFalse(queue._conversational_reply({}, 'default', text, None, task=False))
        finally:
            CURRENT_SCOPE.reset(token)

    async def test_submit_waits_by_fragment_in_private_chats_only(self):
        from bot import request_runtime
        enqueue = AsyncMock(return_value={'id': 'job', 'kind': 'text', 'attempts': 0})
        with patch.object(request_runtime, 'store', return_value=NS(enqueue=enqueue)), \
             patch('bot.request_codec.encode_request', AsyncMock(return_value={'kind': 'request'})), \
             patch('bot.intake.note_accepted'):
            await request_runtime.submit(dict(type='text', chat_id=5, user_id=5, message_id=1, user_message='слушай'))
            self.assertEqual(enqueue.await_args.kwargs['debounce_seconds'], rhythm.INCOMPLETE)
            await request_runtime.submit(dict(type='text', chat_id=5, user_id=5, message_id=2, user_message='Как дела?'))
            self.assertEqual(enqueue.await_args.kwargs['debounce_seconds'], rhythm.QUICK)
            await request_runtime.submit(dict(type='text', chat_id=-100, user_id=5, message_id=3, user_message='слушай'))
            self.assertIsNone(enqueue.await_args.kwargs['debounce_seconds'])

    def test_previous_reply_helper(self):
        import bot.queue as queue
        self.assertEqual(queue._previous_reply([('assistant', 'a'), ('user', 'b')]), 'a')
        self.assertEqual(queue._previous_reply([('user', 'x'), ('assistant', 'a')]), 'a')
        self.assertIsNone(queue._previous_reply([('assistant', 'a'), ('user', 'b'), ('user', 'c')]))
        self.assertIsNone(queue._previous_reply([]))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class ProvisionalExpressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from bot.request_runtime import CURRENT_REQUEST
        from cognition.runtime import CognitiveRuntime, CURRENT_TURN
        from cognition.scope import CURRENT_SCOPE
        from tests.cognition.test_full_model import RecordedInterpreter
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.tokens = [(v, v.set(None)) for v in (CURRENT_TURN, CURRENT_SCOPE, CURRENT_REQUEST)]
        self.runtime = await CognitiveRuntime(self.pool, RecordedInterpreter(), 'active', strict=True,
                                              prepare_budget=.01).initialize(False)
        self.runtime_patch = patch('cognition.runtime.get_runtime', return_value=self.runtime)
        self.runtime_patch.start()

    async def asyncTearDown(self):
        self.runtime_patch.stop()
        await self.runtime.close()
        for var, token in reversed(self.tokens):
            var.reset(token)
        await self.db.__aexit__(None, None, None)

    async def delayed(self, text, kind='neutral', message=1):
        from cognition.types import Perception, PERCEPTION_VERSION
        from tests.cognition.test_full_model import situation
        gate, entered = asyncio.Event(), asyncio.Event()

        async def interpret(ev, **kwargs):
            entered.set()
            await gate.wait()
            return NS(perception=Perception(ev.event_id, PERCEPTION_VERSION, (), situation(ev, kind=kind)))
        self.runtime.interpreter = NS(interpret=interpret)
        turn = await self.runtime.prepare(10, 1, text, message)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(turn.expression_pending)
        return turn, gate

    async def finish(self, gate):
        tasks = tuple(self.runtime.foreground)
        gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)

    async def regulation_record(self, turn):
        from cognition.serialization import object_value
        async with self.pool.acquire() as conn:
            return object_value(await conn.fetchval(
                "SELECT payload FROM cognitive_artifacts WHERE kind='regulation' AND context_id=$1", turn.context_id))

    async def test_clear_loss_is_felt_before_the_interpretation_arrives(self):
        turn, gate = await self.delayed('Мой кот умер сегодня утром', kind='loss')
        plan = turn.expression
        self.assertTrue(plan.provisional)
        self.assertEqual(plan.dominant, 'sadness')
        self.assertTrue(plan.serious)
        self.assertIn('acknowledge_loss', plan.behaviors)
        self.assertEqual((plan.sticker_mood, plan.reaction, plan.disclosure, plan.cause_ids), (None, None, 0., ()))
        self.assertIn('первое впечатление', plan.instruction())
        self.assertEqual((await self.regulation_record(turn))['provisional'], 'loss')
        # The grounded interpretation replaces the first impression.
        await self.finish(gate)
        self.assertTrue(await self.runtime.refresh_expression(turn))
        self.assertFalse(turn.expression.provisional)
        self.assertIsNone((await self.regulation_record(turn))['provisional'])

    async def test_insult_is_answered_dry_not_soft(self):
        turn, gate = await self.delayed('ты тупая железка', kind='conflict')
        self.assertEqual((turn.expression.dominant, turn.expression.tone), ('anger', 'dry and precise'))
        self.assertIn('точнее и суше', turn.expression.instruction())
        await self.finish(gate)

    async def test_unclear_message_keeps_the_neutral_fallback(self):
        turn, gate = await self.delayed('в фильме герой умер, такая драма')
        plan = turn.expression
        self.assertFalse(plan.provisional)
        self.assertIsNone(plan.dominant)
        self.assertEqual((plan.regulation, plan.behaviors, plan.playfulness), ('acknowledge', (), 0.))
        self.assertIsNone((await self.regulation_record(turn))['provisional'])
        await self.finish(gate)

    async def test_first_impression_never_writes_cognitive_state(self):
        turn, gate = await self.delayed('Ураааа, я сдала экзамен!', kind='success')
        self.assertEqual(turn.expression.dominant, 'joy')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_effects WHERE event_id=$1', turn.event_id), 0)
        await self.finish(gate)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class ReplyPathTests(unittest.IsolatedAsyncioTestCase):
    """The real reply path with a durable request: reaction, bubbles, quotes, acknowledgement."""
    from tests.cognition import test_request_delivery as _fixtures
    asyncSetUp = _fixtures.ActiveRequestDeliveryTests.asyncSetUp
    asyncTearDown = _fixtures.ActiveRequestDeliveryTests.asyncTearDown

    async def reply(self, answer, text='Я сдала экзамен!', reaction=None, history=None, pacing=False):
        from contextlib import ExitStack
        from dataclasses import replace
        from telegram.ext import ExtBot
        from bot.queue import process_user_reply
        from cognition.prompting import DialogueTurns
        from cognition.scope import TransportScope
        self.turn.expression = replace(self.turn.expression, reaction=reaction)
        self.events = []
        # Fakes keep Telegram's method names: RetryBot routes durable sends by name.
        async def set_message_reaction(bot, *args, **kwargs):
            self.events.append(('reaction', kwargs['reaction']))
            return True
        async def send_chat_action(bot, *args, **kwargs):
            self.events.append(('typing', kwargs.get('action')))
            return True
        request = dict(chat_id=10, user_id=1, user_name='Synthetic', user_message=text, message_id=1,
                       context=NS(bot=self.bot), is_voice=False, _telegram_scope=TransportScope(10, -1, 'private', 1, 1))
        turns = DialogueTurns(tuple(history or (('assistant', 'Удачи на экзамене!'), ('user', text))))
        with ExitStack() as st:
            st.enter_context(patch('materials.runtime.enabled', return_value=False))
            for target, value in [('bot.request_runtime.prepare_turn', self.turn), ('bot.organizer_commands.handle_natural', False),
                                  ('bot.intent_context.routing_context', []), ('ai.intents.resolve_intent', {}),
                                  ('bot.queue.get_chat_context', ''), ('bot.queue.get_chat_model', 'fake'),
                                  ('utils.chat_history.get_dialogue_turns', turns),
                                  ('bot.queue.generate_response_stream', (answer, False, [], []))]:
                st.enter_context(patch(target, new=AsyncMock(return_value=value)))
            st.enter_context(patch.object(ExtBot, 'send_chat_action', new=send_chat_action))
            st.enter_context(patch.object(ExtBot, 'set_message_reaction', new=set_message_reaction))
            st.enter_context(patch('bot.queue.TTS_ENABLED', False))
            st.enter_context(patch.dict(os.environ, {'ARTI_HUMAN_PACING': '1' if pacing else '0'}))
            st.enter_context(patch('bot.human_rhythm.plan_pacing', new=lambda user, parts, elapsed: [.01] * len(parts)))
            await process_user_reply(request, self.bot)

    async def test_small_talk_reacts_first_then_bubbles_without_a_quote(self):
        await self.reply('Ого, поздравляю!\n\nКак будешь отмечать?', reaction='🔥', pacing=True)
        self.assertEqual([c['text'] for c in self.calls], ['Ого, поздравляю!', 'Как будешь отмечать?'])
        self.assertTrue(all(c.get('reply_to_message_id') is None for c in self.calls))
        self.assertEqual(self.events[0], ('reaction', '🔥'))
        self.assertIn(('typing', 'typing'), self.events)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('SELECT ordinal,state,receipt FROM arti_request_sends ORDER BY ordinal')
        self.assertEqual(len(rows), 3)

    async def test_resumed_request_without_a_stored_decision_never_reacts(self):
        # A request resumed after a crash (or started before H3) keeps its send ordinals.
        self.job['attempts'] = 2
        await self.reply('Ого!\n\nРасскажи подробнее.', reaction='🔥')
        self.assertNotIn('reaction', [kind for kind, _ in self.events])
        self.assertEqual(len(self.calls), 2)

    async def test_task_reply_stays_one_quoted_message(self):
        await self.reply('Вот план:\n1. Купить билеты\n2. Забронировать отель\n\nГотово.', text='Составь план поездки')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['reply_to_message_id'], 1)

    async def test_a_waiting_newer_message_keeps_the_quote(self):
        from bot.request_runtime import store
        await store().enqueue('text', 10, -1, 'later-message', {})
        await self.reply('Ого!\n\nРасскажи подробнее.')
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(c['reply_to_message_id'] == 1 for c in self.calls))

    async def test_pure_thanks_gets_only_a_reaction(self):
        await self.reply('Пожалуйста!', text='спасибо', history=(('assistant', 'Готово: напомню в восемь.'), ('user', 'спасибо')))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events, [('reaction', '❤')])

    async def test_thanks_after_a_question_is_answered_in_text(self):
        await self.reply('Пожалуйста!', text='спасибо', history=(('assistant', 'Напомнить ещё и утром?'), ('user', 'спасибо')))
        self.assertEqual([c['text'] for c in self.calls], ['Пожалуйста!'])


if __name__ == '__main__':
    unittest.main()
