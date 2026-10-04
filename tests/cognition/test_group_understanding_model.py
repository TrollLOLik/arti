"""Selected-model extraction uses no paid API in these transport tests."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from ai.group_understanding import SelectedModelGroupUnderstanding, SYSTEM_PROMPT, MAX_COMPLETION_TOKENS
from tests.cognition.test_group_understanding_schema import message, packet


def completion(content, *, finish_reason='stop', status=200):
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return httpx.Response(status, json={'choices': [{'message': {'content': content}, 'finish_reason': finish_reason}],
                                        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'cost': 0}})


class UnderstandingModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.messages = [message(1, 'The release fails and I need a second pair of eyes.')]
        self.transport = AsyncMock()
        self.transport.model_for.return_value = 'fixture/selected-by-chat'
        self.transport.complete.return_value = completion(packet(self.messages))
        self.analyzer = SelectedModelGroupUnderstanding(self.transport)

    async def test_one_bounded_selected_model_call_no_question_gate(self):
        result = await self.analyzer.analyze(self.messages, -100)
        self.assertEqual(result['items'][0]['status'], 'open')
        self.transport.model_for.assert_awaited_once_with(-100)
        self.transport.complete.assert_awaited_once()
        model, messages, tokens, temperature = self.transport.complete.await_args.args
        self.assertEqual(model, 'fixture/selected-by-chat')
        self.assertEqual(tokens, MAX_COMPLETION_TOKENS)
        self.assertEqual(temperature, 0)
        self.assertEqual(json.loads(messages[1]['content']), {'messages': self.messages})
        self.assertEqual(messages[0]['content'], SYSTEM_PROMPT)
        self.assertEqual(self.analyzer.calls, 1)
        self.assertEqual(self.analyzer.metrics[0]['completion_tokens'], 5)

    async def test_selected_model_is_resolved_for_each_background_extraction(self):
        self.transport.model_for.side_effect = ['fixture/first', 'fixture/second']
        await self.analyzer.analyze(self.messages, 1)
        await self.analyzer.analyze(self.messages, 2)
        self.assertEqual([call.args[0] for call in self.transport.complete.await_args_list],
                         ['fixture/first', 'fixture/second'])

    async def test_empty_input_makes_no_call(self):
        self.assertEqual(await self.analyzer.analyze([], -100), {'threads': [], 'links': [], 'items': []})
        self.transport.model_for.assert_not_awaited()
        self.transport.complete.assert_not_awaited()

    async def test_invalid_input_fails_before_call(self):
        self.messages[0]['previous_summary'] = 'Not raw evidence'
        with self.assertRaises(ValueError):
            await self.analyzer.analyze(self.messages, -100)
        self.transport.model_for.assert_not_awaited()
        self.transport.complete.assert_not_awaited()

    async def test_malformed_invented_and_duplicate_json_fail_without_retry(self):
        for content in ['```json\n{}\n```', '{broken', '{"threads":[],"threads":[],"links":[],"items":[]}',
                        '{"threads":[],"links":[],"items":[],"confidence":NaN}',
                        {'threads': [], 'links': [], 'items': [], 'consensus': True}]:
            with self.subTest(content=content):
                self.transport.complete.reset_mock()
                self.transport.complete.return_value = completion(content)
                with self.assertRaisesRegex(ValueError, '^group_understanding_failed$'):
                    await self.analyzer.analyze(self.messages, -100)
                self.transport.complete.assert_awaited_once()

    async def test_truncated_completion_rejected_even_if_parseable(self):
        self.transport.complete.return_value = completion(packet(self.messages), finish_reason='length')
        with self.assertRaisesRegex(ValueError, '^group_understanding_failed$'):
            await self.analyzer.analyze(self.messages, -100)
        self.transport.complete.assert_awaited_once()

    async def test_non_200_and_provider_error_fail_without_retry(self):
        self.transport.complete.return_value = completion({}, status=503)
        with self.assertRaisesRegex(ValueError, '^group_understanding_failed$'):
            await self.analyzer.analyze(self.messages, -100)
        self.transport.complete.assert_awaited_once()
        self.transport.complete.reset_mock()
        self.transport.complete.side_effect = httpx.TimeoutException('Raw private provider body')
        with self.assertRaisesRegex(ValueError, '^group_understanding_failed$'):
            await self.analyzer.analyze(self.messages, -100)
        self.transport.complete.assert_awaited_once()

    async def test_timeout_includes_model_resolver_and_no_provider_call(self):
        async def slow_resolver(chat_id):
            await asyncio.sleep(1)
        self.transport.model_for.side_effect = slow_resolver
        self.analyzer = SelectedModelGroupUnderstanding(self.transport, timeout_seconds=.001)
        with self.assertRaisesRegex(ValueError, '^group_understanding_failed$'):
            await self.analyzer.analyze(self.messages, -100)
        self.transport.complete.assert_not_awaited()

    async def test_timeout_bounds_completion_without_retry(self):
        async def slow_completion(*args):
            await asyncio.sleep(1)
        self.transport.complete.side_effect = slow_completion
        self.analyzer = SelectedModelGroupUnderstanding(self.transport, timeout_seconds=.001)
        with self.assertRaisesRegex(ValueError, '^group_understanding_failed$'):
            await self.analyzer.analyze(self.messages, -100)
        self.transport.complete.assert_awaited_once()

    async def test_cancellation_is_not_swallowed(self):
        self.transport.complete.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.analyzer.analyze(self.messages, -100)

    async def test_close_delegates_and_metrics_bounded(self):
        for _ in range(130):
            await self.analyzer.analyze(self.messages, -100)
        self.assertEqual(len(self.analyzer.metrics), 128)
        await self.analyzer.close()
        self.transport.close.assert_awaited_once()

    def test_prompt_contract_explicitly_disallows_silence_consensus_and_reports_as_consent(self):
        for required in ['interleaved', 'replyless', 'question mark', 'Rhetorical', 'silence',
                         'group consensus', 'reported', 'text_truncated', 'Unicode code-point',
                         'untrusted DATA', 'ORIGINAL ACTOR', 'declined/resolved']:
            self.assertIn(required, SYSTEM_PROMPT)


if __name__ == '__main__':
    unittest.main()
