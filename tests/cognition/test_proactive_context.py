"""Synthetic public conversation/policy contracts; no live models or services."""
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx

from ai.group_participation import GroupJudgement, OpenRouterGroupJudge, SelectedModelGroupJudge
from cognition.group_context import (build_frame, PUBLIC_PACKET_BYTE_LIMIT,
                                     QUESTION_EVIDENCE_LIMIT, REPLY_CHAIN_LIMIT)
from cognition.group_policy import GroupPolicy, PolicyRepository


AT = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def message(mid, text, owner=1, reply=None, **extra):
    return dict(message_id=mid, source_id=f'public:{mid}', event_id=mid, text=text,
                owner_id=owner, reply_to_id=reply, sender_kind='user', directed=False,
                is_bot=False, at=(AT + timedelta(seconds=mid)).isoformat(), **extra)


def frame(messages, **kwargs):
    return build_frame(1, -10, 5, messages, **kwargs)


def contribution(**changes):
    return dict(dict(message_id=100, source_ids=['public:1'], kind='open_question',
                     channel='text', text='Try sorting the input.', delivery_status='delivered',
                     at=AT.isoformat(), feedback=[]), **changes)


def judgement(**changes):
    return dict(dict(action='abstain', reason='uncertain', usefulness=.1, interruption=.1,
                     confidence=.9, evidence_ids=['public:1'], channel='text', defer_seconds=30), **changes)


class ProactiveContextTests(unittest.TestCase):
    def test_uncertainty_and_shared_curiosity_are_not_answers(self):
        for text in ('Не знаю', 'Тоже интересно', 'Понятия не имею', 'Не уверен',
                     'Я тоже хочу узнать', 'Сам пытаюсь разобраться', "I don't know",
                     'No idea', 'Also wondering', 'Same question', 'Not sure', 'Still looking'):
            with self.subTest(text=text):
                f = frame([message(1, 'Как починить импорт?'), message(2, text, 2, 1)])
                self.assertEqual(f.questions[1]['status'], 'open')
                self.assertEqual(f.questions[1]['outcome'], 'unknown')
                self.assertEqual(f.questions[1]['evidence'][-1]['kind'], 'uncertainty')

    def test_proposed_answer_is_not_proven_resolution(self):
        f = frame([message(1, 'Как отсортировать строки?'),
                   message(2, 'Используй sorted(items)', 2, 1)])
        self.assertEqual(f.questions[1]['status'], 'possibly_answered')
        self.assertEqual(f.questions[1]['outcome'], 'unknown')
        self.assertEqual(f.questions[1]['evidence'][0]['source_id'], 'public:2')

    def test_bot_answer_and_bot_success_claim_do_not_resolve(self):
        for text in ('Используй sorted(items)', 'Вопрос снят, разобрались'):
            with self.subTest(text=text):
                bot = message(2, text, 1, 1); bot.update(is_bot=True, sender_kind='bot')
                f = frame([message(1, 'Как отсортировать строки?'), bot])
                self.assertEqual(f.questions[1]['status'], 'possibly_answered')
                self.assertEqual(f.questions[1]['outcome'], 'unknown')
                self.assertEqual(f.questions[1]['evidence'][-1]['kind'], 'bot_reply')

    def test_thanks_is_reception_not_success(self):
        f = frame([message(1, 'Как починить импорт?'), message(2, 'Попробуй новый формат', 2, 1),
                   message(3, 'Спасибо', 1, 2)])
        self.assertNotEqual(f.questions[1]['status'], 'closed')
        self.assertEqual(f.questions[1]['outcome'], 'unknown')

    def test_owner_uncertainty_after_proposal_reopens_question(self):
        f = frame([message(1, 'Как починить импорт?'), message(2, 'Попробуй новый формат', 2, 1),
                   message(3, 'Не знаю, всё ещё не работает', 1, 2)])
        self.assertEqual(f.questions[1]['status'], 'open')
        self.assertEqual(f.questions[1]['outcome'], 'unknown')

    def test_owner_resolution_follows_reply_chain_not_lexical_branch(self):
        f = frame([message(1, 'Как починить импорт?'), message(2, 'Как починить импорт?', 2),
                   message(3, 'Как починить импорт?', 1), message(4, 'Попробуй JSON', 3, 1),
                   message(5, 'Спасибо, теперь работает', 1, 4)])
        self.assertEqual(f.messages[0]['branch'], f.messages[1]['branch'])
        self.assertEqual(f.questions[1]['status'], 'closed')
        self.assertEqual(f.questions[1]['outcome'], 'resolved')
        self.assertEqual(f.questions[1]['evidence'][-1]['kind'], 'owner_resolution')
        self.assertEqual(f.questions[2]['status'], 'open')
        self.assertEqual(f.questions[3]['status'], 'open')

    def test_other_participants_cannot_confirm_success_for_question_owner(self):
        f = frame([message(1, 'Как починить импорт?'), message(2, 'Вопрос снят, разобрались', 2, 1)])
        self.assertEqual(f.questions[1]['status'], 'possibly_answered')
        self.assertEqual(f.questions[1]['outcome'], 'unknown')
        self.assertEqual(f.questions[1]['evidence'][-1]['kind'], 'participant_resolution')

    def test_refusal_is_owner_scoped_and_is_not_a_successful_outcome(self):
        f = frame([message(1, 'Как починить импорт?'), message(2, 'Как починить импорт?', 2),
                   message(3, 'Не поднимай эту тему', 1, 1)])
        self.assertEqual(f.questions[1]['status'], 'closed')
        self.assertEqual(f.questions[1]['outcome'], 'declined')
        self.assertEqual(f.questions[2]['status'], 'open')

    def test_someone_elses_refusal_does_not_close_either_owners_other_question(self):
        f = frame([message(1, 'Как починить импорт?'), message(2, 'Где взять данные?', 2),
                   message(3, 'Не вмешивайся', 2, 1)])
        self.assertEqual(f.questions[1]['status'], 'open')
        self.assertEqual(f.questions[2]['status'], 'open')
        self.assertEqual(f.questions[1]['evidence'][-1]['kind'], 'participant_refusal')

    def test_unthreaded_resolution_needs_unambiguous_owner_context(self):
        f = frame([message(1, 'Как исправить ошибку?'), message(2, 'Вопрос снят, разобрались')])
        self.assertEqual(f.questions[1]['outcome'], 'resolved')
        f = frame([message(1, 'Как исправить ошибку?'), message(2, 'Кстати, письмо отправлено'),
                   message(3, 'Вопрос снят, разобрались')])
        self.assertEqual(f.questions[1]['status'], 'open')

    def test_negated_reported_or_questioning_resolution_does_not_close(self):
        for text in ('Не разобрались', 'Вопрос снят?', 'Он сказал: вопрос снят',
                     '«Не поднимай эту тему»', 'Спасибо, ничего не получилось', 'Решили пойти гулять'):
            with self.subTest(text=text):
                f = frame([message(1, 'Как исправить ошибку?'), message(2, text, 1, 1)])
                self.assertNotEqual(f.questions[1]['status'], 'closed')
                self.assertEqual(f.questions[1]['outcome'], 'unknown')

    def test_partial_or_hypothetical_success_never_hard_closes(self):
        for text in ('It works for lists, but the dictionary case is still broken.',
                     'Получилось в примере, но в моём файле ошибка осталась.',
                     'It works in theory; I have not tried it yet.',
                     'Вопрос снят для другого файла, но у меня ещё ошибка'):
            with self.subTest(text=text):
                f = frame([message(1, 'How can we repair this?'), message(2, text, 1, 1)])
                self.assertNotEqual(f.question(1)['status'], 'closed')
                self.assertEqual(f.question(1)['outcome'], 'unknown')

    def test_latest_owner_correction_reopens_reported_success(self):
        f = frame([message(1, 'How can we repair this?'), message(2, 'It works.', 1, 1),
                   message(3, 'Actually, not sure, still trying.', 1, 1)])
        self.assertEqual(f.question(1)['status'], 'open')
        self.assertEqual(f.question(1)['outcome'], 'unknown')
        self.assertEqual(f.question(1)['evidence'][-1]['kind'], 'uncertainty')

    def test_new_owner_reply_revalidates_resolution_without_keyword_dependency(self):
        for text in ('Actually the import still fails.',
                     'I spoke too soon. The same exception is back.',
                     'Поторопился. Импорт снова падает.'):
            for reply in (1, 2):
                with self.subTest(text=text, reply=reply):
                    f = frame([message(1, 'How can we repair this?'), message(2, 'It works.', 1, 1),
                               message(3, text, 1, reply)])
                    self.assertEqual(f.question(1)['status'], 'possibly_answered')
                    self.assertEqual(f.question(1)['outcome'], 'unknown')
                    self.assertEqual(f.question(1)['evidence'][-1]['kind'], 'owner_followup')
                    packet = f.public_packet(1)
                    self.assertTrue({1, 2, 3}.issubset(m['message_id'] for m in packet['messages']))

    def test_unclassified_followup_does_not_revoke_refusal_or_other_owners_resolution(self):
        f = frame([message(1, 'How can we repair this?'), message(2, 'Do not bring this up again', 1, 1),
                   message(3, 'I spoke too soon. The same exception is back.', 1, 2)])
        self.assertEqual(f.question(1)['status'], 'closed')
        self.assertEqual(f.question(1)['outcome'], 'declined')
        f = frame([message(1, 'How can we repair this?'), message(2, 'It works.', 1, 1),
                   message(3, 'I spoke too soon. The same exception is back.', 2, 2)])
        self.assertEqual(f.question(1)['status'], 'closed')
        self.assertEqual(f.question(1)['outcome'], 'resolved')

    def test_explicit_owner_reopening_changes_prior_local_refusal(self):
        f = frame([message(1, 'How can we repair this?'), message(2, 'Do not bring this up again', 1, 1),
                   message(3, "Actually, let's revisit this", 1, 1)])
        self.assertEqual(f.question(1)['status'], 'open')
        self.assertEqual(f.question(1)['outcome'], 'unknown')
        self.assertEqual(f.question(1)['evidence'][-1]['kind'], 'owner_reopened')
        f = frame([message(1, 'Как починить импорт?'), message(2, 'Не вмешивайся', 1, 1),
                   message(3, 'Не знаю', 1, 1)])
        self.assertEqual(f.question(1)['status'], 'closed')
        self.assertEqual(f.question(1)['outcome'], 'declined')

    def test_anchor_terminal_evidence_survives_newer_question_hint_overflow(self):
        messages = [message(1, 'How do we deploy the report?'),
                    message(2, 'Do not bring this up again', 1, 1)]
        messages += [message(i, f'Parallel unrelated question {i}?', 2, 1) for i in range(3, 65)]
        f = frame(messages); packet = f.public_packet(1)
        self.assertLessEqual(len(f.questions), 16)
        self.assertLessEqual(len(f.question_index), 64)
        self.assertEqual(f.question(1)['status'], 'closed')
        self.assertEqual(f.question(1)['outcome'], 'declined')
        self.assertTrue({1, 2}.issubset(m['message_id'] for m in packet['messages']))
        q = next(q for q in packet['questions'] if q['message_id'] == 1)
        self.assertEqual(q['outcome'], 'declined')
        self.assertTrue(any(e['message_id'] == 2 and e['kind'] == 'owner_refusal' for e in q['evidence']))

    def test_persisted_message_channel_is_presented_as_text(self):
        packet = frame([message(1, 'How can we repair this?')],
                       outcomes=[contribution(channel='message')]).public_packet(1)
        self.assertEqual(packet['recent_contributions'][0]['channel'], 'text')

    def test_silence_and_later_activity_do_not_create_resolution_or_feedback(self):
        messages = [message(1, 'Как исправить ошибку?')]
        messages += [message(i, f'Отдельный разговор {i}', 2) for i in range(2, 60)]
        f = frame(messages)
        self.assertEqual(f.questions[1]['status'], 'open')
        self.assertEqual(f.questions[1]['outcome'], 'unknown')
        self.assertEqual(f.norms['evidence_participants'], 0)
        self.assertFalse(f.norms['silence_is_rejection'])

    def test_old_anchor_and_nested_reply_chain_survive_recent_window(self):
        messages = [message(1, 'Нужен импорт с сохранением порядка?')]
        messages += [message(i, f'Отдельный разговор {i}', 2) for i in range(2, 58)]
        messages += [message(58, 'Это для загрузчика', 1, 1), message(59, 'А типы тоже сохранять', 1, 58)]
        f = frame(messages)
        packet = f.public_packet(59)
        ids = {m['message_id'] for m in packet['messages']}
        self.assertTrue({1, 58, 59}.issubset(ids))
        self.assertFalse(packet['context_bounds']['reply_chain_incomplete'])
        self.assertTrue(packet['branch_ids_are_hints'])
        packet = f.public_packet(1)
        self.assertTrue({1, 58, 59}.issubset(m['message_id'] for m in packet['messages']))

    def test_old_unresolved_question_and_latest_changes_are_both_preserved(self):
        messages = [message(1, 'Как починить импорт?'), message(2, 'Не знаю', 2, 1)]
        messages += [message(i, f'Новая реплика {i}', 2) for i in range(3, 64)]
        messages += [message(64, 'Теперь переходим к отчёту', 2)]
        packet = frame(messages).public_packet(64)
        ids = {m['message_id'] for m in packet['messages']}
        self.assertTrue({1, 2, 64}.issubset(ids))
        self.assertEqual(packet['questions'][0]['status'], 'open')
        self.assertEqual(packet['questions'][0]['evidence'][0]['message_id'], 2)

    def test_latest_owner_confirmation_is_kept_even_after_many_replies(self):
        messages = [message(1, 'Как починить импорт?')]
        messages += [message(i, f'Попробуй вариант {i}', 2, 1) for i in range(2, 63)]
        messages += [message(63, 'Спасибо, теперь работает', 1, 62), message(64, 'Другой разговор', 3)]
        f = frame(messages); packet = f.public_packet(1)
        self.assertEqual(len(f.questions[1]['evidence']), QUESTION_EVIDENCE_LIMIT)
        self.assertIn(63, {m['message_id'] for m in packet['messages']})
        self.assertEqual(packet['questions'][0]['outcome'], 'resolved')
        self.assertFalse(packet['questions'][0]['evidence_complete'])

    def test_reply_chain_is_bounded_and_missing_context_is_explicit(self):
        messages = [message(i, f'Продолжение {i}', reply=i - 1 if i > 1 else None) for i in range(1, 65)]
        packet = frame(messages).public_packet(64)
        self.assertTrue(packet['context_bounds']['reply_chain_incomplete'])
        self.assertTrue(set(range(64 - REPLY_CHAIN_LIMIT, 65)).issubset(m['message_id'] for m in packet['messages']))
        missing = frame(messages).public_packet(-1)
        self.assertFalse(missing['context_bounds']['anchor_available'])
        cyclic = frame([message(1, 'a', reply=2), message(2, 'b', reply=1)]).public_packet(2)
        self.assertTrue(cyclic['context_bounds']['reply_chain_incomplete'])

    def test_whole_packet_has_utf8_budget_and_only_public_allowlisted_fields(self):
        messages = [message(i, '😀Я\\\"\n' * 2500 + '?', owner=i % 3 + 1,
                            private_memory='DM_SECRET', internal_credentials='SECRET') for i in range(1, 65)]
        outcomes = [contribution(message_id=100 + i, text='😀' * 5000,
                                 at=(AT + timedelta(seconds=i)).isoformat(), private_memory='OUTCOME_SECRET',
                                 feedback=[dict(source_id=f'feedback:{i}', owner_id=2, signal=.7, private='PRIVATE')])
                    for i in range(8)]
        f = frame(messages, outcomes=outcomes)
        f.norms['private_profile'] = 'SECRET_PROFILE'
        f.norms['by_kind']['PRIVATE_KIND'] = dict(receptivity='PRIVATE_VALUE')
        packet = f.public_packet(1); rendered = json.dumps(packet, ensure_ascii=False)
        self.assertLessEqual(len(rendered.encode('utf-8')), PUBLIC_PACKET_BYTE_LIMIT)
        self.assertLessEqual(len(packet['messages']), 32)
        self.assertIn(1, {m['message_id'] for m in packet['messages']})
        self.assertIn(64, {m['message_id'] for m in packet['messages']})
        self.assertTrue(any(m['text_truncated'] for m in packet['messages']))
        self.assertNotIn('SECRET', rendered); self.assertNotIn('PRIVATE', rendered)
        self.assertNotIn('_at', rendered)
        ids = {m['message_id'] for m in packet['messages']}
        self.assertTrue(all(e['message_id'] in ids for q in packet['questions'] for e in q['evidence']))

    def test_packet_never_fetches_an_anchor_outside_64_message_frame(self):
        messages = [message(1, 'FORGOTTEN_OR_OUTSIDE_WINDOW')]
        messages += [message(i, f'Обычная реплика {i}') for i in range(2, 67)]
        packet = frame(messages).public_packet(1)
        self.assertFalse(packet['context_bounds']['anchor_available'])
        self.assertNotIn('FORGOTTEN_OR_OUTSIDE_WINDOW', str(packet))
        self.assertNotIn(1, {m['message_id'] for m in packet['messages']})

    def test_delivered_unknown_and_feedback_have_no_invented_success(self):
        f = frame([message(1, 'Как исправить ошибку?')], suppression_epoch=7,
                  outcomes=[contribution(feedback=[dict(source_id='reaction:1', owner_id=1, signal=.7)]),
                            contribution(message_id=None, delivery_status='delivery_unknown',
                                         at=(AT + timedelta(seconds=1)).isoformat())])
        self.assertEqual(f.suppression_epoch, 7)
        rows = f.public_packet(1)['recent_contributions']
        self.assertEqual({r['delivery_status'] for r in rows}, {'delivered', 'delivery_unknown'})
        self.assertTrue(all(r['effect'] == 'unknown' for r in rows))
        self.assertEqual(rows[1]['feedback'][0]['source_id'], 'reaction:1')
        self.assertEqual(f.questions[1]['outcome'], 'unknown')


class ProactiveQuietPolicyTests(unittest.TestCase):
    def test_unknown_timezone_fails_closed_only_for_unsolicited_work(self):
        policy = GroupPolicy(mode='useful', full_visibility=True)
        for kind in ('initiative', 'followup', 'open_question'):
            self.assertEqual(policy.reason(AT, kind), 'unknown_timezone')
        self.assertIsNone(policy.reason(AT, 'reminder'))
        self.assertEqual(replace(policy, disabled=True).reason(AT, 'reminder'), 'disabled')
        self.assertEqual(replace(policy, paused_until=(AT + timedelta(hours=1)).isoformat()).reason(AT, 'reminder'), 'paused')

    def test_overnight_quiet_window_exact_boundaries_and_day_rollover(self):
        policy = GroupPolicy(mode='useful', full_visibility=True, timezone='Europe/Berlin')
        # October is UTC+02: quiet 23:00 to 09:00 local.
        for instant, reason in [('2026-10-04T20:59:59+00:00', None),
                                ('2026-10-04T21:00:00+00:00', 'quiet_hours'),
                                ('2026-10-05T06:59:59+00:00', 'quiet_hours'),
                                ('2026-10-05T07:00:00+00:00', None)]:
            self.assertEqual(policy.reason(datetime.fromisoformat(instant)), reason, instant)

    def test_daytime_quiet_window_and_explicitly_disabled_window(self):
        policy = GroupPolicy(mode='useful', full_visibility=True, timezone='UTC', quiet_start=12, quiet_end=14)
        self.assertEqual(policy.reason(AT), 'quiet_hours')
        self.assertIsNone(policy.reason(AT.replace(hour=14)))
        self.assertIsNone(replace(policy, quiet_end=12).reason(AT))
        self.assertEqual(replace(policy, timezone=None, quiet_end=12).reason(AT), 'unknown_timezone')

    def test_dst_fall_back_both_fold_occurrences_are_quiet(self):
        policy = GroupPolicy(mode='useful', full_visibility=True, timezone='America/New_York', quiet_start=1, quiet_end=2)
        for instant in ('2026-11-01T05:30:00+00:00', '2026-11-01T06:30:00+00:00'):
            self.assertEqual(policy.reason(datetime.fromisoformat(instant)), 'quiet_hours')
        self.assertIsNone(policy.reason(datetime.fromisoformat('2026-11-01T07:00:00+00:00')))

    def test_dst_spring_gap_uses_actual_local_time(self):
        policy = GroupPolicy(mode='useful', full_visibility=True, timezone='America/New_York', quiet_start=1, quiet_end=3)
        self.assertEqual(policy.reason(datetime.fromisoformat('2026-03-08T06:59:59+00:00')), 'quiet_hours')
        self.assertIsNone(policy.reason(datetime.fromisoformat('2026-03-08T07:00:00+00:00')))


class ProactiveJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_judge_receives_current_resolution_and_outcome_context(self):
        judge = OpenRouterGroupJudge(key='offline', client=NS())
        judge.request = AsyncMock(return_value=judgement())
        f = frame([message(1, 'Как исправить ошибку?'), message(2, 'Тоже интересно', 2, 1)],
                  outcomes=[contribution(delivery_status='delivery_unknown')])
        result = await judge.assess(f, dict(message_id=1, kind='open_question', mode='useful'))
        self.assertEqual(result.action, 'abstain')
        system, data, _, _ = judge.request.await_args.args
        for wording in ('CURRENT relevance', 'reply_to_id', 'Не знаю', 'refusal', 'silence is unknown',
                        'never permission', 'do not fill gaps', 'not proof the problem was solved'):
            self.assertIn(wording, system)
        self.assertEqual(data['conversation']['questions'][0]['status'], 'open')
        self.assertEqual(data['conversation']['recent_contributions'][0]['delivery_status'], 'delivery_unknown')

    async def test_validated_public_feedback_provenance_can_be_cited(self):
        judge = OpenRouterGroupJudge(key='offline', client=NS())
        judge.request = AsyncMock(return_value=judgement(evidence_ids=['reaction:1']))
        f = frame([message(1, 'Как исправить ошибку?')],
                  outcomes=[contribution(feedback=[dict(source_id='reaction:1', owner_id=1, signal=-.7)])])
        self.assertEqual((await judge.assess(f, dict(message_id=1))).evidence_ids, ('reaction:1',))
        judge.request.return_value = judgement(evidence_ids=['private:1'])
        with self.assertRaisesRegex(ValueError, 'Unsupported public evidence'):
            await judge.assess(f, dict(message_id=1))

    async def test_composer_instructions_do_not_claim_success_from_delivery_or_silence(self):
        judge = OpenRouterGroupJudge(key='offline', client=NS())
        judge.request = AsyncMock(return_value={'text': ''})
        result = await judge.compose(frame([message(1, 'Как исправить ошибку?')]),
                                     dict(message_id=1), GroupJudgement.parse(judgement(), {'public:1'}))
        self.assertEqual(result, '')
        system = judge.request.await_args.args[0]
        self.assertIn('without explicit public evidence', system)
        self.assertIn('A delivered message or silence proves none', system)

    async def test_openrouter_optional_failure_and_malformed_output_use_one_call(self):
        for failure in ('http', 'malformed', 'timeout'):
            with self.subTest(failure=failure):
                client = NS(post=AsyncMock())
                if failure == 'http':
                    client.post.side_effect = httpx.HTTPError('provider detail must not leak')
                elif failure == 'timeout':
                    client.post.side_effect = TimeoutError('provider detail must not leak')
                else:
                    client.post.return_value = NS(raise_for_status=lambda: None,
                                                  json=lambda: {'choices': [{'message': {'content': 'invalid JSON'}}]})
                judge = OpenRouterGroupJudge(key='offline', client=client)
                with self.assertRaisesRegex(ValueError, '^group_provider_failed$'):
                    await judge.request('instructions', {}, 100)
                self.assertEqual(judge.calls, 1)
                client.post.assert_awaited_once()

    async def test_selected_model_optional_failure_and_malformed_output_use_one_call(self):
        for failure in ('http', 'malformed', 'timeout'):
            with self.subTest(failure=failure):
                transport = NS(model_for=AsyncMock(return_value='offline-model'), complete=AsyncMock())
                if failure == 'timeout':
                    transport.complete.side_effect = TimeoutError('detail')
                else:
                    transport.complete.return_value = NS(status_code=503 if failure == 'http' else 200,
                        json=lambda: {'choices': [{'message': {'content': 'invalid JSON'}}]})
                judge = SelectedModelGroupJudge(transport=transport)
                with self.assertRaisesRegex(ValueError, '^group_provider_failed$'):
                    await judge.request('instructions', {}, 100, -10)
                self.assertEqual(judge.calls, 1)
                transport.complete.assert_awaited_once()

    async def test_resolution_and_relevance_abstention_reasons_cannot_authorize_speech(self):
        for reason in ('resolved', 'topic_refused', 'stale_context', 'duplicate_contribution', 'missing_context'):
            self.assertEqual(GroupJudgement.parse(judgement(reason=reason), {'public:1'}).action, 'abstain')
            with self.assertRaisesRegex(ValueError, 'contradicts'):
                GroupJudgement.parse(judgement(action='speak', reason=reason), {'public:1'})

    async def test_opt_out_holds_chat_fence_and_update_in_one_transaction(self):
        events = []
        class Context:
            def __init__(self, name, value=None): self.name = name; self.value = value
            async def __aenter__(self): events.append('enter:' + self.name); return self.value
            async def __aexit__(self, *args): events.append('exit:' + self.name)
        async def execute(sql, *args): events.append(sql)
        conn = NS(execute=execute, transaction=lambda: Context('transaction'))
        pool = NS(acquire=lambda: Context('connection', conn))
        await PolicyRepository(pool).opt_out(-10, 1, True)
        self.assertEqual(events[:2], ['enter:connection', 'enter:transaction'])
        self.assertIn('pg_advisory_xact_lock', events[2])
        self.assertIn('INSERT INTO group_participant_settings', events[3])
        self.assertIn('UPDATE group_candidates', events[4])
        self.assertEqual(events[-2:], ['exit:transaction', 'exit:connection'])
