"""Synthetic behavior contracts and actual group pipeline, with no live models."""
import asyncio
import os
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cognition.affect import initial_state, expression
from cognition.regulation import regulate
from cognition.situations import Situation
from cognition.types import ContextKey, EmotionEpisode, ExpressionPlan
from cognition.group_context import contextual_candidate, build_frame
from cognition.group_policy import GroupPolicy
from tests.cognition.test_affect import AT


def mixed_state():
    state=initial_state(ContextKey('arti',10),AT)
    return replace(state,episodes=(
        EmotionEpisode('joy','progress','g1','help_user',1,'joy',.3,.9,AT,1200),
        EmotionEpisode('fear','risk','g2','help_user',1,'fear',.2,.9,AT,2400)))


class BehavioralExpressionTests(unittest.TestCase):
    def test_mixed_causes_survive_without_one_emotion_sticker(self):
        state=mixed_state(); before=asdict(state); plan=expression(state)
        self.assertTrue(plan.mixed_affect); self.assertIsNone(plan.sticker_mood)
        self.assertEqual(set(plan.cause_ids),{'progress','risk'})
        self.assertIn('do not force one mood',plan.instruction())
        self.assertEqual(asdict(state),before)

    def test_same_affect_different_situations_choose_different_actions(self):
        state=mixed_state()
        _,loss=regulate(state,Situation(kind='loss',modality='interaction'))
        _,task=regulate(state,Situation(kind='request',modality='interaction'))
        _,success=regulate(state,Situation(kind='success',modality='interaction'))
        self.assertEqual(loss.behaviors,('acknowledge_loss','offer_choice'))
        self.assertEqual(task.behaviors,('answer_task','practical_step'))
        self.assertEqual(success.behaviors,('recognize_progress','listen'))
        self.assertNotEqual(loss.instruction(),task.instruction())

    def test_uncertainty_clarifies_without_attributing_hostility(self):
        _,plan=regulate(mixed_state(),Situation(kind='conflict',intention_evidence='ambiguous',modality='interaction'))
        self.assertEqual(plan.behaviors,('ask_one_question',)); self.assertNotIn('boundary',plan.behaviors)

    def test_quoted_loss_does_not_direct_personal_condolences(self):
        _,plan=regulate(mixed_state(),Situation(kind='loss',modality='quoted'))
        self.assertNotIn('acknowledge_loss',plan.behaviors)

    def test_defaults_and_behavior_allowlist(self):
        plan=ExpressionPlan('acknowledge','calm',.5,.5,0.,0.,None,'neutral',(),False)
        self.assertEqual(plan.behaviors,())
        with self.assertRaises(ValueError): replace(plan,behaviors=('invent_user_feelings',))

    def test_contextual_eligibility_needs_no_lexical_trigger(self):
        p=GroupPolicy(mode='useful',full_visibility=True)
        message=dict(text='В третий раз импорт зависает на той же строке',sender_kind='user')
        self.assertTrue(contextual_candidate(None,message,p))
        for change in ({'directed':True},{'addressed_elsewhere':True},{'is_bot':True},{'sender_kind':'chat'}):
            self.assertFalse(contextual_candidate(None,{**message,**change},p))
        self.assertFalse(contextual_candidate(None,message,replace(p,full_visibility=False)))
        self.assertFalse(contextual_candidate(None,message,replace(p,mode='mentions')))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class ContextualPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from tests.cognition.test_full_model import RecordedInterpreter
        from tests.cognition.test_groups import RecordedJudge
        from cognition.runtime import CognitiveRuntime,CURRENT_TURN
        from cognition.scope import CURRENT_SCOPE
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); self.at=AT
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active',clock=lambda:self.at).initialize(False)
        self.judge=RecordedJudge(); self.runtime.groups.judge=self.judge
        self.bot=NS(send_message=AsyncMock(return_value=NS(message_id=901,chat=NS(id=-10))),
            set_message_reaction=AsyncMock(return_value=True),get_chat_member=AsyncMock(return_value=NS(status='member')))
        self.tokens=[(v,v.set(None)) for v in (CURRENT_SCOPE,CURRENT_TURN)]
        async with self.pool.acquire() as conn: await conn.execute('INSERT INTO response_status(chat_id,enabled) VALUES(-10,TRUE)')
        await self.runtime.groups.policies.set(-10,dict(mode='useful',execution='live',full_visibility=True,spacing_seconds=60,daily_limit=20))

    async def asyncTearDown(self):
        for var,token in self.tokens: var.reset(token)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)

    async def observe(self,text,i=1,owner=1):
        from cognition.scope import TransportScope
        return await self.runtime.groups.observe(TransportScope(-10,5,'supergroup',owner,i,False,'user'),text,at=self.at)

    async def tick(self):
        self.at+=timedelta(seconds=60)
        await self.runtime.groups.run_cycle(self.bot)

    async def test_nonlexical_request_reaches_public_arbiter_and_delivery(self):
        await self.runtime.ingest(44,1,'PRIVATE_MUST_NOT_APPEAR',1)
        captured=[]; original=self.judge.assess
        async def assess(frame,candidate):
            captured.append((frame.public_packet(),candidate))
            return await original(frame,candidate)
        self.judge.assess=assess
        text='В третий раз импорт зависает на той же строке'
        cid=await self.observe(text); await self.tick()
        self.assertEqual(self.judge.assess_calls,1); self.bot.send_message.assert_awaited_once()
        self.assertEqual(captured[0][1]['kind'],'contextual')
        self.assertIn(text,str(captured)); self.assertNotIn('PRIVATE_MUST_NOT_APPEAR',str(captured))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT status FROM group_candidates WHERE context_id=$1',cid),'delivered')

    async def test_semantic_abstention_is_terminal_and_never_composes(self):
        self.judge.action='abstain'; self.judge.reason='no_added_value'
        await self.observe('По дороге встретились у старого здания'); await self.tick()
        self.assertEqual(self.judge.assess_calls,1); self.assertEqual(self.judge.compose_calls,0)
        self.bot.send_message.assert_not_awaited()

    async def test_contextual_sweep_is_bounded_and_reserves_lexical_budget(self):
        await self.runtime.groups.policies.set(-10,dict(assessment_hourly_limit=2))
        self.judge.action='abstain'; self.judge.reason='no_added_value'
        for i in range(1,8): await self.observe('Текст без вопросительного знака '+str(i),i)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM group_candidates WHERE kind='contextual'"),1)
        await self.tick(); self.assertEqual(self.judge.assess_calls,1)
        self.at+=timedelta(seconds=91)
        await self.observe('Ещё одно наблюдение без явного обращения',8); await self.tick()
        self.assertEqual(self.judge.assess_calls,1)
        await self.observe('Как можно починить импорт?',9); await self.tick()
        self.assertEqual(self.judge.assess_calls,2)

    async def test_timeout_abstains_without_delivery_or_unbounded_wait(self):
        async def slow(*args): await asyncio.Event().wait()
        self.judge.assess=slow
        cid=await self.observe('В третий раз импорт зависает на той же строке')
        with patch('cognition.proactivity.ASSESS_TIMEOUT_SECONDS',.01): await self.tick()
        self.bot.send_message.assert_not_awaited()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT status FROM group_candidates WHERE context_id=$1',cid),'cancelled')

    async def test_opt_out_prevents_contextual_candidate(self):
        await self.runtime.groups.policies.opt_out(-10,1,True)
        await self.observe('В третий раз импорт зависает на той же строке'); await self.tick()
        self.assertEqual(self.judge.assess_calls,0)

    async def test_interpreted_loss_changes_prepared_prompt_behavior(self):
        from tests.cognition.test_full_model import situation
        text='Synthetic loss report'
        _,_,ev=await self.runtime.ingest(10,1,text,1)
        self.runtime.interpreter.frames[text]=situation(ev,kind='loss',modality='interaction')
        turn=await self.runtime.prepare(10,1,text,1)
        self.assertEqual(turn.expression.behaviors,('acknowledge_loss','offer_choice'))
        self.assertIn('without forced optimism',turn.expression.instruction())
