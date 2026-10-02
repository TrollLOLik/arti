"""Long recorded trajectories, assessed against runtime invariants, not model output."""
import os
import unittest
from unittest.mock import AsyncMock,patch
from tools.evaluate_long_conversations import run_scenario,SCENARIOS


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class LongConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.no_env=patch('tests.support.database.dotenv_values',return_value={}); self.no_env.start()
        self.no_load=patch('dotenv.load_dotenv',return_value=False); self.no_load.start()
        self.network=patch('httpx.AsyncClient.send',new=AsyncMock(side_effect=AssertionError('network_disabled'))); self.network.start()
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
    async def asyncTearDown(self):
        await self.db.__aexit__(None,None,None); self.no_env.stop(); self.no_load.stop(); self.network.stop()
    async def check_scenario(self,name):
        row=await run_scenario(self.pool,name)
        self.assertGreaterEqual(row['turns'],20)
        self.assertGreater(row['invariants'],row['turns'])
        self.assertEqual(row['failed_invariants'],[])
        self.assertEqual(row['passed'],row['invariants'])
        self.assertEqual(sum(c['assessed'] for c in row['invariant_counts'].values()),row['invariants'])
    async def test_correction_and_owner_trajectory(self): await self.check_scenario(SCENARIOS[0])
    async def test_affect_preferences_and_erasure_trajectory(self): await self.check_scenario(SCENARIOS[1])
    async def test_roleplay_scene_trajectory(self): await self.check_scenario(SCENARIOS[2])
    async def test_durable_delivery_trajectory(self): await self.check_scenario(SCENARIOS[3])
