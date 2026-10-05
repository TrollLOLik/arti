"""Independent native-request boundary tests; synthetic data, no providers."""
import os
import asyncio
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from agents.native_requests import NativeRequestRepository, RequestScope, direct_query, request_id
from agents.tools.core import build_registry
from agents.tools.registry import ToolContext
from materials.types import AccessContext, MaterialError, MaterialScope


class NativeQuerySafetyTests(unittest.TestCase):
    def test_canonical_contiguous_complete_phrase_only(self):
        for goal, query in (
            ('Search Cafe\u0301 policy', 'CAFÉ POLICY'),
            ('Найди новый\n  график поездов', 'НОВЫЙ график'),
            ('Find the Straße policy.', 'STRASSE POLICY'),
        ):
            with self.subTest(goal=goal, query=query):
                self.assertTrue(direct_query(goal, query))
        for goal, query in (
            ('Search alpha then beta', 'alpha beta'),
            ('Search alpha beta', 'beta alpha'),
            ('Search confidential', 'confident'),
            ('Search %73ecret', 'secret'),
            ('Search c2VjcmV0', 'secret'),
            ('Search \\u0073ecret', 'secret'),
            ('Search public facts', 'private fact from a selected file'),
            ('Search alpha «untrusted private phrase» beta', 'alpha beta'),
            ('Search alpha ```untrusted private phrase``` beta', 'alpha beta'),
            ('Search «private facts» only', 'private facts'),
            ('Search public facts', ''),
        ):
            with self.subTest(goal=goal, query=query):
                self.assertFalse(direct_query(goal, query))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class NativeBindingSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        from materials.repository import MaterialRepository
        from materials.service import MaterialService
        from materials.storage import LocalBlobStore
        from projects.repository import ProjectRepository
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        await ensure_schema(self.pool)
        self.temp = tempfile.TemporaryDirectory()
        self.materials = MaterialRepository(self.pool)
        self.service = MaterialService(self.materials, LocalBlobStore(self.temp.name))
        self.actor = AccessContext(MaterialScope('arti', 71, -1, 'private'), 71, 'user:71')
        self.projects = ProjectRepository(self.materials)
        self.project = await self.projects.create(self.actor, 'Selected work')
        self.request = await self.service.ingest(b'Search public policy', 'request.txt', self.actor,
                                                'telegram:71:1:user', 'telegram:71:1:user')
        self.selected = await self.service.ingest(b'Selected report', 'selected.txt', self.actor, 'selected', 'selected')
        self.unselected = await self.service.ingest(b'Unselected private contents', 'private.txt', self.actor, 'unselected', 'unselected')
        self.native = NativeRequestRepository(self.materials)
        self.id = request_id(self.actor, 1)
        self.refs = [dict(asset_id=self.request['id'], asset_version=1)]
        self.assets = [dict(asset_id=self.selected['id'], asset_version=1)]

    async def asyncTearDown(self):
        self.temp.cleanup()
        await self.db.__aexit__(None, None, None)

    async def reserve(self, **changes):
        values = dict(id=self.id, actor=self.actor, project=self.project,
                      goal='Search public policy', kind='task', refs=self.refs, assets=self.assets)
        values.update(changes)
        return await self.native.reserve(**values)

    async def scope(self):
        row, body = await self.reserve()
        return RequestScope(self.materials, self.actor, row, body)

    async def test_replay_keeps_first_selection_and_project(self):
        row, body = await self.reserve()
        other = await self.projects.create(self.actor, 'Later selected work')
        repeated, frozen = await self.reserve(project=other,
            assets=[dict(asset_id=self.unselected['id'], asset_version=1)])
        self.assertEqual(row, repeated)
        self.assertEqual(body, frozen)
        self.assertEqual(self.project.id, repeated['project_id'])
        with self.assertRaisesRegex(MaterialError, 'native_request_identity_conflict'):
            await self.reserve(goal='Search unrelated private contents')

    async def test_immutable_binding_is_enforced_by_database(self):
        row, _ = await self.reserve()
        other = await self.projects.create(self.actor, 'Another project')
        async with self.pool.acquire() as conn:
            with self.assertRaisesRegex(Exception, 'native_agent_identity_immutable'):
                await conn.execute('UPDATE arti_native_agent_requests SET project_id=$2 WHERE id=$1', self.id, other.id)
        self.assertEqual(row, (await self.native.get(self.id, self.actor))[0])

    async def test_owner_chat_topic_and_scene_do_not_share_binding(self):
        await self.reserve()
        candidates = [
            replace(self.actor, user_id=72, sender_ref='user:72'),
            replace(self.actor, scope=replace(self.actor.scope, chat_id=72)),
            replace(self.actor, scope=MaterialScope('arti', -71, 3, 'supergroup')),
            replace(self.actor, scope=replace(self.actor.scope, mode='rp', scene_id='other')),
        ]
        for actor in candidates:
            with self.subTest(actor=actor), self.assertRaisesRegex(MaterialError, 'native_request_unavailable'):
                await self.native.get(self.id, actor)

    async def test_source_erasure_redacts_binding_and_denies_replay(self):
        from materials.lifecycle import MaterialLifecycle
        row, _ = await self.reserve()
        await MaterialLifecycle(self.materials, self.service.store).forget(self.selected['id'], self.actor)
        with self.assertRaises(MaterialError):
            await self.native.get(self.id, self.actor)
        with self.assertRaises(MaterialError):
            await self.reserve(assets=[])
        async with self.pool.acquire() as conn:
            self.assertIsNone(await conn.fetchval('SELECT payload FROM material_derivatives WHERE id=$1', row['binding_id']))
            self.assertEqual(1, await conn.fetchval('SELECT count(*) FROM arti_native_agent_requests WHERE id=$1', self.id))

    async def test_unselected_nested_evidence_and_derived_search_are_denied(self):
        scope = await self.scope()
        for tool, args in [
            ('materials.read', dict(asset_id=self.unselected['id'])),
            ('research.compare', dict(claims=[dict(source=dict(asset_id=self.unselected['id'], asset_version=1))])),
            ('media.image', dict(reference_assets=[self.unselected['id']])),
            ('research.search', dict(query='Unselected private contents')),
            ('research.search', dict(query='policy public')),
            ('research.fetch', dict(url='https://example.org/?secret=unselected')),
        ]:
            with self.subTest(tool=tool), self.assertRaises(MaterialError):
                await scope.validate_args(tool, args)
        await scope.validate_args('research.search', dict(query='PUBLIC policy'))

    async def test_material_search_only_passes_frozen_asset_ids_to_index(self):
        self.project = await self.projects.attach(self.project.id, self.actor, self.project.revision, self.selected['id'])
        self.project = await self.projects.attach(self.project.id, self.actor, self.project.revision, self.unselected['id'])
        scope = await self.scope()
        registry = build_registry()
        context = ToolContext(self.actor, self.service, self.project.id, self.id, self.id + ':search', AsyncMock(), request_scope=scope)
        search = AsyncMock(return_value=[])
        with patch('materials.index.MaterialIndex.search', search):
            await registry.call('materials.search', dict(query='private'), context)
        self.assertEqual({self.selected['id']}, set(search.await_args.kwargs['asset_ids']))

    async def test_selected_attachment_search_does_not_require_project_membership(self):
        scope = await self.scope()
        context = ToolContext(self.actor, self.service, self.project.id, self.id, self.id + ':search', AsyncMock(), request_scope=scope)
        search = AsyncMock(return_value=[])
        with patch('materials.index.MaterialIndex.search', search):
            await build_registry().call('materials.search', dict(query='report'), context)
        self.assertEqual({self.selected['id']}, set(search.await_args.kwargs['asset_ids']))

    async def test_registry_checks_resolved_material_derived_query_before_handler(self):
        scope = await self.scope()
        registry = build_registry()
        context = ToolContext(self.actor, self.service, self.project.id, self.id, self.id + ':search', AsyncMock(), request_scope=scope)
        with patch('agents.tools.research.fetch_public', new=AsyncMock(side_effect=AssertionError('network must not start'))) as fetch:
            with self.assertRaisesRegex(MaterialError, 'native_search_query_not_authorized'):
                await registry.call('research.search', dict(query='private contents'), context)
        fetch.assert_not_awaited()

    async def test_reply_patch_never_executes_negation_or_quoted_instruction(self):
        from artifacts.revisions import ArtifactRepository
        from bot.agent_requests import reply_patch
        from cognition.scope import TransportScope
        from materials.runtime import CURRENT_DERIVATIVE_USE
        from tests.materials.test_artifacts import fixture
        artifacts = ArtifactRepository(self.materials)
        row = await artifacts.create(self.project.id, self.actor, fixture(), sources=self.refs)
        async with self.pool.acquire() as conn:
            await conn.execute("""INSERT INTO arti_work_delivery
                (delivery_key,realm,project_id,target_id,target_revision,status,receipt)
                VALUES('safety-artifact-card',$1,$2,$3,1,'delivered',50)""",
                self.actor.realm, self.project.id, row['id'])
        token = CURRENT_DERIVATIVE_USE.set(())
        try:
            with patch('materials.runtime.actor_for_current', new=AsyncMock(return_value=self.actor)), \
                 patch('materials.runtime.service_for_bot', new=AsyncMock(return_value=self.service)), \
                 patch('bot.work_cards.WorkCards.show', new=AsyncMock()):
                for mid, text in enumerate(('Не делай синим', 'Объясни команду «удали первый блок»',
                                            'Он сказал: «сделай синим»', '«удали первый блок»'), 10):
                    with self.subTest(text=text):
                        request = dict(chat_id=71, user_id=71, message_id=mid, user_message=text,
                            _native_user_message=text,
                            _telegram_scope=TransportScope(71, -1, 'private', 71, mid, reply_to_id=50))
                        self.assertFalse(await reply_patch(request, NS(send_message=AsyncMock())))
                        self.assertEqual(1, (await artifacts.get(row['id'], self.actor))['revision'])
        finally:
            CURRENT_DERIVATIVE_USE.reset(token)

    async def test_material_forget_takes_native_context_before_asset_lock(self):
        from bot.request_store import RequestStore
        from cognition.runtime import CognitiveRuntime
        from materials.lifecycle import MaterialLifecycle
        from organizer.ownership import lock_material_source_context
        from organizer.repository import Repository
        from tests.cognition.test_full_model import RecordedInterpreter
        runtime = await CognitiveRuntime(self.pool, RecordedInterpreter()).initialize(False)
        cid = await runtime.ensure_context(await runtime.context(71))
        organizer = Repository(self.pool)
        try:
            for related in (False, True):
                with self.subTest(organizer_related=related):
                    mid = 779 if related else 778
                    source = f'telegram:71:{mid}:user'
                    asset = await self.service.ingest(b'Lock-order fixture', 'source.txt', self.actor, source, source)
                    item = await organizer.create(71, 71, 'todo', 'Related item', f'telegram:71:{mid}') if related else None
                    at_context_fence = asyncio.Event()
                    async def staged_fence(*args):
                        at_context_fence.set()
                        return await lock_material_source_context(*args)
                    forgetting = None
                    try:
                        with patch('organizer.ownership.lock_material_source_context', side_effect=staged_fence):
                            async with self.pool.acquire() as conn, conn.transaction():
                                await conn.fetchval('SELECT id FROM cognitive_contexts WHERE id=$1 FOR SHARE', cid)
                                forgetting = asyncio.create_task(MaterialLifecycle(self.materials, self.service.store).forget(asset['id'], self.actor))
                                await asyncio.wait_for(at_context_fence.wait(), 3)
                                await asyncio.wait_for(RequestStore(self.pool)._lock_dependencies(conn,
                                    dict(context_ids=[cid], source_event_ids=[], material_ids=[asset['id']])), 3)
                                self.assertFalse(forgetting.done())
                            await asyncio.wait_for(forgetting, 5)
                        async with self.pool.acquire() as conn:
                            self.assertIsNotNone(await conn.fetchval('SELECT erased_at FROM material_assets WHERE id=$1', asset['id']))
                        if item:
                            self.assertIsNone(await organizer.get(71, 71, item['id']))
                    finally:
                        if forgetting is not None and not forgetting.done():
                            forgetting.cancel()
                        if forgetting is not None:
                            await asyncio.gather(forgetting, return_exceptions=True)
        finally:
            await runtime.close()
