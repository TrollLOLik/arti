"""Offline contract tests: semantic hypotheses are grounded, scoped and bounded."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from cognition.group_understanding import (
    MAX_INPUT_BYTES, MAX_MESSAGES, MAX_OUTPUT_BYTES, normalize_messages,
    parse_understanding, retained_sources, understanding_sources, revalidate_understanding, understanding_wire, reconcile_understanding,
)


AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def message(index, text, owner=1, **overrides):
    return dict(source_id=f's{index}', message_id=index, owner_id=owner,
                sender_kind='user', directed=False, reply_to_id=None,
                at=(AT + timedelta(seconds=index)).isoformat(), text=text,
                text_truncated=False, **overrides)


def span(messages, index, quote=None):
    source = next(row for row in messages if row['source_id'] == f's{index}')
    quote = source['text'] if quote is None else quote
    start = source['text'].index(quote)
    return dict(source_id=source['source_id'], start=start, end=start + len(quote), quote=quote)


def thread(messages, index=1, label='Repair deployment'):
    return dict(thread_id=f's{index}', label=label, confidence=.95,
                evidence=[span(messages, index)])


def item(messages, index=1, kind='question', status='open', **overrides):
    row = next(row for row in messages if row['source_id'] == f's{index}')
    result = dict(kind=kind, thread_id=f's{index}', origin_source_id=f's{index}',
                  summary='The speaker needs deployment assistance.', actor_id=row['owner_id'],
                  attribution='speaker', status=status, confidence=.95,
                  evidence=[span(messages, index)], updates=[])
    result.update(overrides)
    return result


def update(messages, index, status, **overrides):
    row = next(row for row in messages if row['source_id'] == f's{index}')
    result = dict(source_id=f's{index}', status=status, actor_id=row['owner_id'],
                  attribution='speaker', confidence=.95, evidence=[span(messages, index)])
    result.update(overrides)
    return result


def packet(messages, origin=1):
    return dict(threads=[thread(messages, origin)], links=[], items=[item(messages, origin)])


class UnderstandingSchemaTests(unittest.TestCase):
    def setUp(self):
        self.messages = [message(1, 'The service still crashes during deployment.'),
                         message(2, 'Try replacing that deployment token.', 2),
                         message(3, 'I verified the deployment; it is fixed now.')]
        self.data = packet(self.messages)

    def parse(self, data=None, messages=None):
        return parse_understanding(self.data if data is None else data,
                                   self.messages if messages is None else messages)

    def test_replyless_interleaved_threads_and_question_without_punctuation(self):
        messages = [message(1, 'Нужна помощь, сервер падает при запуске.'),
                    message(2, 'Поезд задержали, теперь я не успеваю к ужину.', 2),
                    message(3, 'Для сервера замените файл конфигурации.', 3),
                    message(4, 'Ресторан сможет перенести бронь на девять.', 4)]
        data = dict(threads=[thread(messages, 1), thread(messages, 2, 'Dinner reservation')],
                    links=[dict(source_id='s3', target_source_id='s1', thread_id='s1',
                                relation='answer', addressee_ids=[1], confidence=.93,
                                evidence=[span(messages, 3), span(messages, 1)]),
                           dict(source_id='s4', target_source_id='s2', thread_id='s2',
                                relation='answer', addressee_ids=[2], confidence=.91,
                                evidence=[span(messages, 4), span(messages, 2)])],
                    items=[item(messages, 1), item(messages, 2)])
        result = parse_understanding(data, messages)
        self.assertEqual([row['target_source_id'] for row in result['links']], ['s1', 's2'])
        self.assertEqual([row['status'] for row in result['items']], ['open', 'open'])
        self.assertTrue(all(row['reply_to_id'] is None for row in messages))
        self.assertNotIn('?', ''.join(row['text'] for row in messages))

    def test_own_explicit_resolution_requires_later_quote(self):
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved')]
        result = self.parse()['items'][0]
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['initial_status'], 'open')
        self.assertEqual(result['status_scope'], 'actor_only')
        self.assertEqual(result['source_ids'], ['s1', 's3'])

    def test_somebody_elses_answer_does_not_resolve_question(self):
        self.data['items'][0]['updates'] = [update(self.messages, 2, 'resolved')]
        result = self.parse()['items'][0]
        self.assertEqual(result['status'], 'open')
        self.assertEqual(result['updates'][0]['status'], 'unknown')

    def test_reported_resolution_does_not_resolve_question(self):
        self.messages[2]['text'] = 'Someone said that the deployment was fixed.'
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved', attribution='reported')]
        self.assertEqual(self.parse()['items'][0]['status'], 'open')

    def test_bot_and_anonymous_updates_never_prove_human_outcome(self):
        for kind, owner in [('bot', 1), ('chat', None)]:
            with self.subTest(kind=kind):
                self.messages[2].update(sender_kind=kind, owner_id=owner)
                self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved', actor_id=1)]
                self.assertEqual(self.parse()['items'][0]['status'], 'open')

    def test_refusal_persists_across_unrelated_unknown_claim(self):
        messages = [message(1, 'Please help with the deployment.'),
                    message(2, 'Leave this topic alone. I decline further help.'),
                    message(3, 'That apparently worked for him.', 2)]
        data = packet(messages)
        data['items'][0]['updates'] = [update(messages, 2, 'declined'), update(messages, 3, 'resolved')]
        result = parse_understanding(data, messages)
        self.assertEqual(result['items'][0]['status'], 'declined')
        self.assertEqual(retained_sources(result, 2), [])
        self.assertEqual(retained_sources(result, 3), ['s1', 's2', 's3'])

    def test_terminal_states_require_reopened_before_open_or_proposed(self):
        for kind, terminal, resumed in [('question', 'declined', 'open'),
                                        ('question', 'resolved', 'open'),
                                        ('commitment', 'cancelled', 'proposed'),
                                        ('commitment', 'fulfilled', 'accepted'),
                                        ('decision', 'superseded', 'proposed')]:
            with self.subTest(kind=kind, terminal=terminal):
                messages = [message(1, 'I need help with the task.'),
                            message(2, 'I am finished discussing this task.'),
                            message(3, 'Another possible task comes to mind.'),
                            message(4, 'Actually, please reopen the original task.')]
                data = packet(messages)
                data['items'][0].update(kind=kind, status='open' if kind == 'question' else 'proposed')
                data['items'][0]['updates'] = [update(messages, 2, terminal), update(messages, 3, resumed)]
                parsed = parse_understanding(data, messages)
                self.assertEqual(parsed['items'][0]['status'], terminal)
                self.assertEqual(parsed['items'][0]['updates'][-1]['status'], 'unknown')
                self.assertEqual(revalidate_understanding(parsed, messages), parsed)
                data['items'][0]['updates'].append(update(messages, 4, 'reopened'))
                self.assertEqual(parse_understanding(data, messages)['items'][0]['status'], 'reopened')

    def test_refusal_reopens_only_with_supported_own_later_change(self):
        self.messages += [message(4, 'Please help after all; I want to try again.')]
        self.messages[2]['text'] = 'I decline more help on this topic.'
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'declined'),
                                          update(self.messages, 4, 'reopened')]
        self.assertEqual(self.parse()['items'][0]['status'], 'reopened')

    def test_uncertain_updates_cannot_erase_resolution(self):
        self.messages += [message(4, 'Maybe the problem returned; I cannot tell.')]
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved'),
                                          update(self.messages, 4, 'reopened', confidence=.3)]
        self.assertEqual(self.parse()['items'][0]['status'], 'resolved')

    def test_silence_does_not_create_an_update_or_outcome(self):
        result = self.parse()['items'][0]
        self.assertEqual(result['status'], 'open')
        self.assertEqual(result['updates'], [])
        self.data['items'][0]['status'] = 'resolved'
        self.assertEqual(self.parse()['items'][0]['status'], 'open')

    def test_proposal_is_not_accepted_at_its_origin(self):
        self.data['items'][0].update(kind='proposal', status='accepted')
        self.assertEqual(self.parse()['items'][0]['status'], 'proposed')

    def test_reported_or_misattributed_commitment_is_unknown(self):
        for actor, attribution in [(2, 'speaker'), (2, 'reported'), (None, 'unknown')]:
            with self.subTest(actor=actor, attribution=attribution):
                self.data['items'][0].update(kind='commitment', status='accepted',
                                             actor_id=actor, attribution=attribution)
                result = self.parse()['items'][0]
                self.assertEqual(result['status'], 'unknown')
                if attribution == 'speaker':
                    self.assertEqual(result['attribution'], 'unknown')
                    self.assertIsNone(result['actor_id'])

    def test_explicit_own_decision_and_commitment_remain_actor_scoped(self):
        for kind in ['decision', 'commitment']:
            with self.subTest(kind=kind):
                self.data['items'][0].update(kind=kind, status='accepted')
                result = self.parse()['items'][0]
                self.assertEqual(result['status'], 'accepted')
                self.assertEqual(result['status_scope'], 'actor_only')

    def test_rhetorical_stays_distinct_from_genuine_question(self):
        self.messages[0]['text'] = 'Кто бы мог подумать, что сервер снова упадёт?'
        self.data['items'][0] = item(self.messages, status='rhetorical')
        self.data['threads'][0] = thread(self.messages)
        self.assertEqual(self.parse()['items'][0]['status'], 'rhetorical')

    def test_exact_unicode_offsets_not_utf8_bytes(self):
        self.messages[0]['text'] = '🙂 Cafe\u0301: нужен ответ'
        self.data['items'][0] = item(self.messages)
        self.data['threads'][0] = thread(self.messages)
        self.data['items'][0]['evidence'] = [span(self.messages, 1, 'нужен ответ')]
        self.parse()
        self.data['items'][0]['evidence'][0]['start'] += 1
        with self.assertRaisesRegex(ValueError, 'quote'):
            self.parse()

    def test_unicode_normalization_is_not_silently_accepted(self):
        self.messages[0]['text'] = 'Cafe\u0301 needs fixing'
        self.data = packet(self.messages)
        self.data['items'][0]['evidence'][0]['quote'] = 'Café needs fixing'
        with self.assertRaisesRegex(ValueError, 'quote'):
            self.parse()

    def test_invented_sources_and_actors_rejected(self):
        for field, value in [('origin_source_id', 'invented'), ('actor_id', 9999), ('thread_id', 'invented')]:
            with self.subTest(field=field):
                data = copy.deepcopy(self.data)
                data['items'][0][field] = value
                with self.assertRaises(ValueError):
                    self.parse(data)

    def test_all_extra_keys_rejected(self):
        for path in [(), ('threads', 0), ('items', 0), ('items', 0, 'evidence', 0)]:
            with self.subTest(path=path):
                data = copy.deepcopy(self.data)
                obj = data
                for key in path:
                    obj = obj[key]
                obj['consensus'] = True
                with self.assertRaisesRegex(ValueError, 'schema'):
                    self.parse(data)

    def test_duplicate_ids_and_duplicate_spans_rejected(self):
        for key in ['threads', 'items']:
            data = copy.deepcopy(self.data)
            data[key].append(copy.deepcopy(data[key][0]))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'duplicate'):
                self.parse(data)
        self.data['items'][0]['evidence'] *= 2
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.parse()

    def test_future_and_out_of_order_updates_rejected(self):
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved'), update(self.messages, 2, 'open')]
        with self.assertRaisesRegex(ValueError, 'noncausal'):
            self.parse()
        self.data['items'][0]['updates'] = [update(self.messages, 1, 'resolved')]
        with self.assertRaisesRegex(ValueError, 'noncausal'):
            self.parse()

    def test_input_order_cannot_override_actual_time(self):
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved')]
        result = self.parse(messages=list(reversed(self.messages)))
        self.assertEqual(result['items'][0]['status'], 'resolved')
        self.messages[2]['at'] = (AT - timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'noncausal'):
            self.parse()

    def test_update_cannot_borrow_another_sources_quote(self):
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved')]
        self.data['items'][0]['updates'][0]['evidence'] = [span(self.messages, 2)]
        with self.assertRaisesRegex(ValueError, 'misattributed'):
            self.parse()

    def test_truncated_update_cannot_prove_outcome(self):
        self.messages[2]['text_truncated'] = True
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved')]
        self.assertEqual(self.parse()['items'][0]['status'], 'open')

    def test_uncertain_link_loses_target_and_addressees_but_keeps_support(self):
        self.data['links'] = [dict(source_id='s2', target_source_id='s1', thread_id='s1',
                                   relation='answer', addressee_ids=[1], confidence=.5,
                                   evidence=[span(self.messages, 2), span(self.messages, 1)])]
        result = self.parse()['links'][0]
        self.assertEqual(result['relation'], 'unknown')
        self.assertIsNone(result['target_source_id'])
        self.assertEqual(result['addressee_ids'], [])
        self.assertEqual(result['source_ids'], ['s1', 's2'])

    def test_unobserved_and_uncited_addressee_rejected(self):
        self.data['links'] = [dict(source_id='s2', target_source_id=None, thread_id='s1',
                                   relation='question', addressee_ids=[2], confidence=.9,
                                   evidence=[span(self.messages, 2)])]
        with self.assertRaisesRegex(ValueError, 'addressee'):
            self.parse()
        self.data['links'][0]['addressee_ids'] = [1]
        with self.assertRaisesRegex(ValueError, 'addressee'):
            self.parse()

    def test_future_target_or_thread_is_not_a_prior_relation(self):
        self.data['links'] = [dict(source_id='s1', target_source_id='s2', thread_id='s1',
                                   relation='answer', addressee_ids=[], confidence=.9,
                                   evidence=[span(self.messages, 1), span(self.messages, 2)])]
        with self.assertRaisesRegex(ValueError, 'noncausal'):
            self.parse()
        self.data['links'] = []
        self.data['threads'] += [thread(self.messages, 3)]
        self.data['items'][0]['thread_id'] = 's3'
        with self.assertRaisesRegex(ValueError, 'noncausal'):
            self.parse()

    def test_bool_nan_infinity_and_structured_enums_rejected(self):
        for value in [True, float('nan'), float('inf'), -.1, 1.1, '0.9', []]:
            with self.subTest(value=value):
                self.data['items'][0]['confidence'] = value
                with self.assertRaises(ValueError):
                    self.parse()
        self.data['items'][0]['confidence'] = .9
        for key in ['kind', 'attribution', 'status']:
            data = copy.deepcopy(self.data)
            data['items'][0][key] = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.parse(data)

    def test_original_inputs_not_mutated(self):
        before = copy.deepcopy((self.data, self.messages))
        self.parse()
        self.assertEqual((self.data, self.messages), before)

    def test_source_enumerator_includes_all_support_and_anchors(self):
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved')]
        self.data['threads'][0]['evidence'] += [span(self.messages, 2)]
        result = self.parse()
        self.assertEqual(understanding_sources(result), {'s1', 's2', 's3'})
        self.assertEqual(retained_sources(result, 2), [])
        self.assertEqual(retained_sources(result, 3), ['s1', 's3', 's2'])
        self.assertEqual(set(retained_sources(result)), {'s1', 's2', 's3'})

    def test_wire_uses_initial_status_and_preserves_safe_demotions(self):
        self.data['items'][0]['updates'] = [update(self.messages, 2, 'resolved'), update(self.messages, 3, 'resolved')]
        parsed = self.parse()
        wire = understanding_wire(parsed)
        self.assertEqual(wire['items'][0]['status'], 'open')
        self.assertEqual(wire['items'][0]['updates'][0]['status'], 'unknown')
        self.assertNotIn('source_ids', wire['items'][0])
        self.assertEqual(parse_understanding(wire, self.messages), parsed)

    def test_revalidation_reconstructs_normalized_state_from_raw_support(self):
        self.data['items'][0]['updates'] = [update(self.messages, 2, 'resolved'),
                                          update(self.messages, 3, 'resolved')]
        parsed = self.parse()
        self.assertEqual(revalidate_understanding(parsed, self.messages), parsed)

    def test_revalidation_rejects_computed_state_or_source_id_tampering(self):
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved')]
        for key, value in [('status', 'declined'), ('status_scope', 'group_consensus'),
                           ('item_id', 'invented'), ('source_ids', ['s1']), ('actor_id', True)]:
            with self.subTest(key=key):
                parsed = self.parse()
                parsed['items'][0][key] = value
                with self.assertRaises(ValueError):
                    revalidate_understanding(parsed, self.messages)

    def test_revalidation_rejects_raw_schema_and_extra_fields(self):
        with self.assertRaises(ValueError):
            revalidate_understanding(self.data, self.messages)
        parsed = self.parse()
        parsed['items'][0]['arbitrary'] = 'not trusted'
        with self.assertRaises(ValueError):
            revalidate_understanding(parsed, self.messages)

    def test_revalidation_handles_demoted_initial_attribution_and_link(self):
        self.data['items'][0].update(actor_id=2, attribution='speaker')
        self.data['links'] = [dict(source_id='s2', target_source_id='s1', thread_id='s1',
                                   relation='answer', addressee_ids=[1], confidence=.3,
                                   evidence=[span(self.messages, 1), span(self.messages, 2)])]
        parsed = self.parse()
        self.assertEqual(revalidate_understanding(parsed, self.messages), parsed)

    def test_low_confidence_or_truncated_origin_cannot_acquire_strong_outcome(self):
        self.data['items'][0]['updates'] = [update(self.messages, 3, 'resolved')]
        self.data['items'][0]['confidence'] = .3
        self.assertEqual(self.parse()['items'][0]['status'], 'unknown')
        self.data['items'][0]['confidence'] = .9
        self.messages[0]['text_truncated'] = True
        self.assertEqual(self.parse()['items'][0]['status'], 'unknown')

    def test_output_bytes_and_counts_are_bounded(self):
        self.assertLessEqual(len(json.dumps(self.parse(), ensure_ascii=False).encode()), MAX_OUTPUT_BYTES)
        self.data['threads'] *= 13
        with self.assertRaises(ValueError):
            self.parse()
        messages = [message(i, '🙂' * 1200, i) for i in range(1, 5)]
        data = dict(threads=[thread(messages, i) for i in range(1, 5)], links=[],
                    items=[item(messages, i) for i in range(1, 5)])
        with self.assertRaisesRegex(ValueError, 'output_too_large'):
            parse_understanding(data, messages)


class CrossGenerationUnderstandingTests(unittest.TestCase):
    def setUp(self):
        self.messages = [message(1, 'Please help with this deployment.'),
                         message(2, 'I decline more help on this deployment.'),
                         message(3, 'An unrelated new deployment is on my mind.'),
                         message(4, 'Please reopen the original deployment issue.'),
                         message(5, 'I also want to discuss that.', 2)]
        prior_raw = packet(self.messages)
        prior_raw['items'][0]['updates'] = [update(self.messages, 2, 'declined')]
        self.previous = parse_understanding(prior_raw, self.messages)

    def fresh(self, updates=()):
        data = packet(self.messages)
        data['items'][0]['updates'] = list(updates)
        return parse_understanding(data, self.messages)

    def test_previous_declined_cannot_become_open_by_omitting_update(self):
        current = self.fresh()
        self.assertEqual(current['items'][0]['status'], 'open')
        result = reconcile_understanding(self.previous, current, self.messages)
        self.assertEqual(result['items'][0], self.previous['items'][0])
        self.assertEqual(result['items'][0]['status'], 'declined')
        self.assertEqual(revalidate_understanding(result, self.messages), result)

    def test_open_question_survives_empty_output_with_complete_raw_support(self):
        previous = self.fresh()
        result = reconcile_understanding(previous, {'threads': [], 'links': [], 'items': []}, self.messages)
        self.assertEqual(result, previous)
        self.assertEqual(result['items'][0]['status'], 'open')

    def test_accepted_own_commitment_survives_empty_output_across_generations(self):
        messages = [message(1, 'I will deliver the signed report tomorrow.'),
                    message(2, 'The venue has changed.', 2)]
        raw = packet(messages)
        raw['items'][0].update(kind='commitment', status='accepted',
                               summary='The speaker commits to delivering the signed report.')
        previous = parse_understanding(raw, messages)
        empty = {'threads': [], 'links': [], 'items': []}
        first = reconcile_understanding(previous, empty, messages)
        second = reconcile_understanding(first, empty, messages)
        self.assertEqual(first, previous)
        self.assertEqual(second, previous)
        self.assertEqual(second['items'][0]['status'], 'accepted')
        self.assertEqual(second['items'][0]['actor_id'], 1)

    def test_proposed_task_survives_omission_without_accepting_unrelated_question(self):
        raw = packet(self.messages)
        raw['items'][0].update(kind='proposal', status='proposed')
        previous = parse_understanding(raw, self.messages)
        current = parse_understanding(packet(self.messages, 3), self.messages)
        result = reconcile_understanding(previous, current, self.messages)
        states = {row['item_id']: row['status'] for row in result['items']}
        self.assertEqual(states['proposal:s1'], 'proposed')
        self.assertEqual(states['question:s3'], 'open')

    def test_nonterminal_current_reassessment_remains_supported_and_distinct(self):
        previous = self.fresh()
        current = self.fresh([update(self.messages, 4, 'resolved')])
        result = reconcile_understanding(previous, current, self.messages)
        self.assertEqual(result['items'][0]['status'], 'resolved')
        self.assertEqual(result['items'][0]['updates'], current['items'][0]['updates'])

    def test_previous_resolved_survives_empty_model_output(self):
        prior_raw = packet(self.messages)
        prior_raw['items'][0]['updates'] = [update(self.messages, 2, 'resolved')]
        previous = parse_understanding(prior_raw, self.messages)
        result = reconcile_understanding(previous, {'threads': [], 'links': [], 'items': []}, self.messages)
        self.assertEqual(result, previous)

    def test_only_later_own_explicit_reopened_resumes_terminal(self):
        current = self.fresh([update(self.messages, 4, 'reopened')])
        result = reconcile_understanding(self.previous, current, self.messages)
        self.assertEqual(result['items'][0]['status'], 'reopened')
        self.assertEqual(result['items'][0]['source_ids'], ['s1', 's4'])

    def test_unknown_other_actor_reported_and_incidental_open_preserve_refusal(self):
        for followup in [update(self.messages, 3, 'unknown'),
                         update(self.messages, 3, 'open'),
                         update(self.messages, 4, 'reopened', confidence=.3),
                         update(self.messages, 4, 'reopened', attribution='reported'),
                         update(self.messages, 5, 'reopened')]:
            with self.subTest(followup=followup):
                current = self.fresh([followup])
                self.assertEqual(reconcile_understanding(self.previous, current, self.messages)
                                 ['items'][0]['status'], 'declined')

    def test_an_older_reopening_cannot_override_a_later_refusal(self):
        prior_raw = packet(self.messages)
        prior_raw['items'][0]['updates'] = [update(self.messages, 4, 'declined')]
        previous = parse_understanding(prior_raw, self.messages)
        current = self.fresh([update(self.messages, 3, 'reopened')])
        self.assertEqual(reconcile_understanding(previous, current, self.messages)
                         ['items'][0]['status'], 'declined')

    def test_missing_old_evidence_omits_instead_of_claiming_open_or_terminal(self):
        messages = [row for row in self.messages if row['source_id'] != 's2']
        current = parse_understanding(packet(messages), messages)
        result = reconcile_understanding(self.previous, current, messages)
        self.assertEqual(result['items'], [])
        result = reconcile_understanding(self.previous, {'threads': [], 'links': [], 'items': []}, messages)
        self.assertEqual(result['items'], [])

    def test_changed_or_truncated_decisive_quote_cannot_be_carried_forward(self):
        for change in [dict(text='A completely different statement.'), dict(text_truncated=True)]:
            with self.subTest(change=change):
                messages = copy.deepcopy(self.messages)
                messages[1].update(change)
                current = parse_understanding(packet(messages), messages)
                self.assertEqual(reconcile_understanding(self.previous, current, messages)['items'], [])

    def test_complete_terminal_source_group_is_atomic_within_retention_budget(self):
        self.assertEqual(retained_sources(self.previous, 1), [])
        self.assertEqual(set(retained_sources(self.previous, 2)), {'s1', 's2'})
        self.previous['threads'][0]['evidence'].append(span(self.messages, 3))
        self.previous['threads'][0]['source_ids'].append('s3')
        self.assertEqual(retained_sources(self.previous, 2), [])
        self.assertEqual(set(retained_sources(self.previous, 3)), {'s1', 's2', 's3'})

    def test_terminal_source_groups_precede_new_open_items(self):
        other = parse_understanding(packet(self.messages, 3), self.messages)
        combined = dict(threads=other['threads'] + self.previous['threads'], links=[],
                        items=other['items'] + self.previous['items'])
        self.assertEqual(retained_sources(combined, 2), ['s1', 's2'])

    def test_output_budget_retains_whole_terminal_objects_and_never_partial_updates(self):
        messages = []
        for index in range(1, 34, 2):
            messages.extend([message(index, 'Please help with this task. ' + 'x' * 120),
                             message(index + 1, 'I decline further help on this task. ' + 'x' * 120)])
        def projection(indices):
            return parse_understanding(dict(
                threads=[thread(messages, index) for index in indices], links=[],
                items=[item(messages, index, updates=[update(messages, index + 1, 'declined')])
                       for index in indices]), messages)
        previous = projection(range(1, 16, 2))
        current = projection(range(17, 32, 2))
        result = reconcile_understanding(previous, current, messages)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), MAX_OUTPUT_BYTES)
        self.assertLessEqual(len(result['threads']), 12)
        self.assertLessEqual(len(result['items']), 24)
        self.assertLess(len(result['items']), len(previous['items']) + len(current['items']))
        by_id = {row['item_id']: row for row in result['items']}
        for previous_item in previous['items']:
            self.assertEqual(by_id[previous_item['item_id']], previous_item)
        self.assertEqual(revalidate_understanding(result, messages), result)

    def test_reconciliation_does_not_mutate_inputs(self):
        current = self.fresh()
        before = copy.deepcopy((self.previous, current, self.messages))
        reconcile_understanding(self.previous, current, self.messages)
        self.assertEqual((self.previous, current, self.messages), before)

    def test_first_generation_requires_normalized_output_and_no_previous(self):
        current = self.fresh()
        self.assertEqual(reconcile_understanding(None, current, self.messages), current)
        with self.assertRaises(ValueError):
            reconcile_understanding(None, packet(self.messages), self.messages)


class RawMessageTests(unittest.TestCase):
    def test_input_rejects_old_summaries_extra_keys_and_oversized_packets(self):
        source = message(1, 'Some raw content')
        for extra in ['summary', 'private_memory', 'previous_understanding']:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'schema'):
                normalize_messages([{**source, extra: 'Old model output'}])
        with self.assertRaises(ValueError):
            normalize_messages([message(i, 'x') for i in range(1, MAX_MESSAGES + 2)])
        with self.assertRaisesRegex(ValueError, 'input_too_large'):
            normalize_messages([message(i, '🙂' * 2400) for i in range(1, 10)])
        self.assertEqual(MAX_INPUT_BYTES, 48000)

    def test_text_keeps_exact_spaces_and_unicode(self):
        source = message(1, '  Cafe\u0301\n🙂  ')
        self.assertEqual(normalize_messages([source])[0]['text'], source['text'])

    def test_actors_timestamps_flags_and_duplicate_sources_strict(self):
        for overrides in [dict(owner_id=True), dict(message_id=False), dict(directed=1),
                          dict(text_truncated='false'), dict(at='2026-01-01'),
                          dict(sender_kind='chat'), dict(sender_kind=[]), dict(text='x' * 2401)]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                normalize_messages([{**message(1, 'x'), **overrides}])
        source = message(1, 'x')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            normalize_messages([source, source])


if __name__ == '__main__':
    unittest.main()
