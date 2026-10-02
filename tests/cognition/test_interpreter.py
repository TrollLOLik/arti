import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from cognition.interpreter import InterpreterFailure, OpenRouterInterpreter
from cognition.serialization import dump
from tests.cognition.test_affect import appraisal, event, perception


class InterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, handler, attempts=1):
        client = OpenRouterInterpreter('synthetic-key-for-mocked-transport', max_attempts=attempts)
        await client.close()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    def result(self, body=None, finish='stop'):
        ev = event()
        body = body if body is not None else json.dumps({'appraisals':json.loads(dump(perception(ev, appraisal())))['appraisals']})
        return {'choices':[{'message':{'content':body}, 'finish_reason':finish}],
                'usage':{'prompt_tokens':100, 'completion_tokens':200, 'cost':0}}

    async def test_verified_contract_and_explicit_model_budget(self):
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200,json=self.result())
        client = await self.client(handler)
        result = await client.interpret(event())
        self.assertEqual(result.perception.event_id, 'e1')
        self.assertEqual(captured[0]['model'], 'stealth/space-bunny-alpha')
        self.assertEqual(captured[0]['max_tokens'],8192)
        self.assertEqual(captured[0]['reasoning']['effort'],'medium')

    async def test_malformed_output_retry_is_bounded_and_counted(self):
        responses = [self.result('{"appraisals":', 'length'), self.result()]
        client = await self.client(lambda request:httpx.Response(200,json=responses.pop(0)), attempts=2)
        with patch('cognition.interpreter.asyncio.sleep',new=AsyncMock()):
            result = await client.interpret(event())
        self.assertEqual(result.attempts,2)
        self.assertEqual(result.prompt_tokens,200)
        self.assertEqual(result.completion_tokens,400)

    async def test_hallucinated_source_rejected(self):
        ev = event()
        body = json.dumps({'appraisals':json.loads(dump(perception(ev,appraisal(evidence_ids=('invented',)))))['appraisals']})
        client = await self.client(lambda request:httpx.Response(200,json=self.result(body)))
        with self.assertRaises(InterpreterFailure) as ctx:
            await client.interpret(ev)
        self.assertEqual(ctx.exception.code,'invalid_perception')
        self.assertEqual(ctx.exception.metrics['attempts'],1)

    async def test_truncated_output_is_not_applied(self):
        client = await self.client(lambda request:httpx.Response(200,json=self.result('{',finish='length')))
        with self.assertRaises(InterpreterFailure) as ctx:
            await client.interpret(event())
        self.assertEqual(ctx.exception.code,'output_truncated')

    async def test_provider_error_never_escapes_as_raw_payload(self):
        client = await self.client(lambda request:httpx.Response(403,json={'error':{'message':'private text and credential'}}))
        with self.assertRaises(InterpreterFailure) as ctx:
            await client.interpret(event())
        self.assertEqual(str(ctx.exception),'provider_rejected')

    async def test_http_200_error_envelope_is_a_bounded_provider_failure(self):
        client = await self.client(lambda request:httpx.Response(200,json={'error':{'message':'private detail'}}))
        with self.assertRaises(InterpreterFailure) as ctx:
            await client.interpret(event())
        self.assertEqual(ctx.exception.code,'provider_unavailable')
        self.assertNotIn('private',str(ctx.exception))

    async def test_timeout_is_not_a_user_emotion(self):
        def handler(request):
            raise httpx.ReadTimeout('synthetic timeout',request=request)
        client = await self.client(handler)
        with self.assertRaises(InterpreterFailure) as ctx:
            await client.interpret(event())
        self.assertEqual(ctx.exception.code,'timeout')

    def test_missing_probability_mass_stays_unknown(self):
        from cognition.interpreter import complete_uncertainty
        ev = event()
        original = appraisal(probability=.7)
        result = complete_uncertainty(perception(ev,original))
        result.validate_for(ev,__import__('cognition.types',fromlist=['DEFAULT_GOALS']).DEFAULT_GOALS)
        self.assertEqual(result.appraisals[0],original)
        self.assertAlmostEqual(result.appraisals[1].probability,.3)
        self.assertEqual(result.appraisals[1].confidence,0.)
        self.assertEqual(result.appraisals[1].congruence,0.)

    def test_overfull_distribution_is_not_normalized(self):
        from cognition.interpreter import complete_uncertainty
        ev = event()
        result = complete_uncertainty(perception(ev,appraisal(probability=.7),appraisal(probability=.7)))
        with self.assertRaises(ValueError):
            result.validate_for(ev,__import__('cognition.types',fromlist=['DEFAULT_GOALS']).DEFAULT_GOALS)
