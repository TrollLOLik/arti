import asyncio,os,tempfile,unittest
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from materials.types import AccessContext,MaterialScope,MaterialError
from materials.extractors.basic import BasicExtractor
from projects.repository import ProjectRepository
from projects.types import ProjectUse
from materials.sharing import ShareGrant,share_copy

@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class ProjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        from materials.repository import MaterialRepository
        from materials.service import MaterialService
        from materials.storage import LocalBlobStore
        from materials.lifecycle import MaterialLifecycle
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); await ensure_schema(self.pool)
        self.temp=tempfile.TemporaryDirectory(); self.repo=MaterialRepository(self.pool)
        self.service=MaterialService(self.repo,LocalBlobStore(self.temp.name)); self.lifecycle=MaterialLifecycle(self.repo,self.service.store)
        self.actor=AccessContext(MaterialScope('arti',-100,4,'supergroup'),7,'user:7')
        self.projects=ProjectRepository(self.repo); self.project=await self.projects.create(self.actor,'Alpha','Read invoices')
    async def asyncTearDown(self): self.temp.cleanup(); await self.db.__aexit__(None,None,None)
    async def test_roles_cas_revocation_and_separate_project_selection(self):
        second=await self.projects.create(self.actor,'Beta')
        self.assertEqual(second.id,(await self.projects.current(self.actor)).id)
        p=await self.projects.member(self.project.id,self.actor,1,8,'editor')
        editor=replace(self.actor,user_id=8,sender_ref='user:8')
        await self.projects.select(p.id,editor)
        self.assertEqual('editor',(await self.projects.current(editor)).role)
        with self.assertRaises(MaterialError): await self.projects.member(p.id,editor,p.revision,9,'manager')
        with self.assertRaises(MaterialError): (await self.projects.get(p.id,editor)).require('approve')
        use=ProjectUse(p.id,editor,self.projects,p.access_generation)
        await self.projects.member(p.id,self.actor,p.revision,8,'remove')
        with self.assertRaises(MaterialError): await use.validate()
        with self.assertRaises(MaterialError): await self.projects.get(p.id,replace(self.actor,scope=replace(self.actor.scope,topic_id=5)))
    async def test_concurrent_edit_archive_resume_and_history(self):
        results=await asyncio.gather(self.projects.edit(self.project.id,self.actor,1,goal='one'),self.projects.edit(self.project.id,self.actor,1,goal='two'),return_exceptions=True)
        self.assertEqual(1,sum(isinstance(r,MaterialError) for r in results))
        p=await self.projects.get(self.project.id,self.actor)
        use=ProjectUse(p.id,self.actor,self.projects,p.access_generation)
        p=await self.projects.status(p.id,self.actor,p.revision,'archived')
        self.assertIsNone(await self.projects.current(self.actor))
        with self.assertRaises(MaterialError): await use.validate()
        p=await self.projects.status(p.id,self.actor,p.revision,'active')
        self.assertEqual(p.id,(await self.projects.select(p.id,self.actor)).id)
        async with self.pool.acquire() as conn: self.assertEqual(4,await conn.fetchval('SELECT COUNT(*) FROM arti_project_revisions WHERE project_id=$1',p.id))
    async def test_membership_does_not_share_private_original(self):
        private=AccessContext(MaterialScope('arti',55,-1,'private'),7,'user:7')
        asset=await self.service.ingest(b'PRIVATE ORIGINAL','note.txt',private,'private','private')
        with self.assertRaises(MaterialError): await self.projects.attach(self.project.id,self.actor,1,asset['id'])
        row,rev=await self.repo.read(asset['id'],private)
        grant=ShareGrant(7,asset['id'],1,row['generation'],rev['sha256'],self.actor.scope.key,'request-1',datetime.now(timezone.utc)+timedelta(minutes=10))
        copy=await share_copy(self.service,private,self.actor,grant)
        p=await self.projects.attach(self.project.id,self.actor,1,copy['id'])
        p=await self.projects.member(p.id,self.actor,p.revision,8,'viewer')
        viewer=replace(self.actor,user_id=8,sender_ref='user:8')
        self.assertEqual('current',(await self.projects.materials_for(p.id,viewer))[0]['status'])
        with self.assertRaises(MaterialError): await self.repo.read(asset['id'],viewer)
        await self.service.extract(copy['id'],self.actor,BasicExtractor())
        await self.lifecycle.forget(asset['id'],private)
        self.assertEqual('unavailable',(await self.projects.materials_for(p.id,viewer))[0]['status'])
        async with self.pool.acquire() as conn:
            self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM material_block_index'))
            self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM material_extractions WHERE payload IS NOT NULL'))
    async def test_share_source_change_audience_change_and_non_author_are_denied(self):
        private=AccessContext(MaterialScope('arti',55,-1,'private'),7,'user:7')
        asset=await self.service.ingest(b'secret','note.txt',private,'private','private'); row,rev=await self.repo.read(asset['id'],private)
        grant=ShareGrant(7,asset['id'],1,0,rev['sha256'],self.actor.scope.key,'request',datetime.now(timezone.utc)+timedelta(minutes=10))
        for altered in (replace(grant,author_id=8),replace(grant,destination_scope_key='changed'),replace(grant,expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))):
            with self.assertRaises(MaterialError): await share_copy(self.service,private,self.actor,altered)
        copy=await share_copy(self.service,private,self.actor,grant)
        same=await share_copy(self.service,private,self.actor,grant); self.assertEqual(copy['id'],same['id'])
        await self.service.revise(asset['id'],b'new secret','note.txt',private,1)
        with self.assertRaises(MaterialError): await self.repo.read(copy['id'],self.actor)
        with self.assertRaises(MaterialError): await share_copy(self.service,private,self.actor,grant)

    async def test_reviewed_publication_restart_source_revocation_and_project_delete(self):
        from projects.publication import ProjectPublication
        asset=await self.service.ingest(b'Invoice 1200','note.txt',self.actor,'source','source')
        p=await self.projects.attach(self.project.id,self.actor,1,asset['id'])
        destination=replace(self.actor,scope=replace(self.actor.scope,topic_id=5))
        publication=ProjectPublication(self.service)
        preview=await publication.preview(p.id,self.actor,p.revision,destination,'explicit-request')
        self.assertEqual('note.txt',preview['inputs'][0]['filename'])
        target=await publication.publish(preview['id'],self.actor)
        repeated=await ProjectPublication(self.service).publish(preview['id'],self.actor)
        self.assertEqual(target.id,repeated.id)
        self.assertEqual(1,len(await self.projects.materials_for(target.id,destination)))
        copy_id=(await self.projects.materials_for(target.id,destination))[0]['asset_id']
        await self.projects.status(p.id,self.actor,p.revision,'deleted')
        with self.assertRaises(MaterialError): await self.projects.get(target.id,destination)
        with self.assertRaises(MaterialError): await self.repo.read(copy_id,destination)
        with self.assertRaises(MaterialError): await publication.publish(preview['id'],self.actor)
        async with self.pool.acquire() as conn: self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM arti_project_publications WHERE payload IS NOT NULL'))

    async def test_publication_snapshot_change_and_foreign_material_are_refused(self):
        from projects.publication import ProjectPublication
        publication=ProjectPublication(self.service); destination=replace(self.actor,scope=replace(self.actor.scope,topic_id=5))
        preview=await publication.preview(self.project.id,self.actor,1,destination,'request')
        await self.projects.edit(self.project.id,self.actor,1,goal='changed')
        with self.assertRaises(MaterialError): await publication.publish(preview['id'],self.actor)
        stranger=replace(self.actor,user_id=8,sender_ref='user:8')
        asset=await self.service.ingest(b'owned by 8','note.txt',stranger,'8','8')
        p=await self.projects.get(self.project.id,self.actor)
        p=await self.projects.attach(p.id,self.actor,p.revision,asset['id'])
        with self.assertRaises(MaterialError): await publication.preview(p.id,self.actor,p.revision,destination,'cannot-grant-for-8')

    async def test_accepted_head_is_separate_from_latest_candidate_and_forget_cleans_reasons(self):
        from materials.derivatives import DerivativeRepository
        from materials.types import EvidenceRef
        asset=await self.service.ingest(b'Invoice 1200','note.txt',self.actor,'result','result')
        eid,bundle=await self.service.extract(asset['id'],self.actor,BasicExtractor()); block=bundle.blocks[0]
        ref=EvidenceRef(asset['id'],1,eid,block.block_id,block.locator); derivatives=DerivativeRepository(self.repo)
        first=await derivatives.save(self.actor,'report',{'value':'1200'},[ref])
        second=await derivatives.save(self.actor,'report',{'value':'proposal 1200'},[ref])
        p=await self.projects.result(self.project.id,self.actor,1,'report',first,status='accepted',reason='Selected invoice 1200')
        p=await self.projects.result(p.id,self.actor,p.revision,'report',second)
        results=await self.projects.results_for(p.id,self.actor)
        self.assertEqual(first,next(r['id'] for r in results if r['selected']))
        await self.lifecycle.forget(asset['id'],self.actor)
        results=await self.projects.results_for(p.id,self.actor)
        self.assertTrue(all(r['availability']=='stale_or_revoked' and r['reason'] is None for r in results))
        async with self.pool.acquire() as conn:
            self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM arti_project_result_candidates WHERE reason IS NOT NULL'))
            self.assertEqual(0,await conn.fetchval("SELECT COUNT(*) FROM arti_project_revisions WHERE kind='result' AND payload IS NOT NULL"))

    async def test_telegram_create_duplicate_archive_resume_explicit_id_and_role_error(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,patch
        from bot.project_commands import project_command
        message=SimpleNamespace(text='/project new "Gamma" "Goal"',message_id=99,chat_id=-100,reply_text=AsyncMock())
        with patch('bot.project_commands.enabled',return_value=True),patch('bot.project_commands.actor_for_current',new=AsyncMock(return_value=self.actor)),patch('bot.project_commands.service_for_bot',new=AsyncMock(return_value=self.service)):
            await project_command(SimpleNamespace(effective_message=message),SimpleNamespace())
            p=await self.projects.current(self.actor)
            await project_command(SimpleNamespace(effective_message=message),SimpleNamespace())
            self.assertEqual(p.id,(await self.projects.current(self.actor)).id)
            message.text='/project archive version=1'; await project_command(SimpleNamespace(effective_message=message),SimpleNamespace())
            self.assertIsNone(await self.projects.current(self.actor))
            message.text='/project resume '+p.id+' version=2'; await project_command(SimpleNamespace(effective_message=message),SimpleNamespace())
            self.assertEqual('active',(await self.projects.current(self.actor)).status)
            message.text='/project edit version=1 "conflict"'; await project_command(SimpleNamespace(effective_message=message),SimpleNamespace())
            self.assertIn('Версия проекта изменилась',message.reply_text.call_args.args[0])

    async def test_source_project_deleted_between_load_and_create_cannot_publish_metadata(self):
        from projects.publication import ProjectPublication
        from unittest.mock import patch
        publication=ProjectPublication(self.service); destination=replace(self.actor,scope=replace(self.actor.scope,topic_id=5))
        preview=await publication.preview(self.project.id,self.actor,1,destination,'request')
        original=publication.projects.create
        async def delete_then_create(*args,**kwargs):
            await self.projects.status(self.project.id,self.actor,1,'deleted')
            return await original(*args,**kwargs)
        with patch.object(publication.projects,'create',side_effect=delete_then_create):
            with self.assertRaises(MaterialError): await publication.publish(preview['id'],self.actor)
        async with self.pool.acquire() as conn: self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM arti_projects WHERE scope_key=$1',destination.scope.key))

    async def test_publication_erasure_does_not_revoke_another_authors_copy(self):
        from projects.publication import ProjectPublication
        publication=ProjectPublication(self.service); destination=replace(self.actor,scope=replace(self.actor.scope,topic_id=5))
        preview=await publication.preview(self.project.id,self.actor,1,destination,'request')
        await publication.publish(preview['id'],self.actor)
        private=AccessContext(MaterialScope('arti',56,-1,'private'),8,'user:8')
        asset=await self.service.ingest(b'Owner 8 independent','note.txt',private,'eight','eight'); row,rev=await self.repo.read(asset['id'],private)
        dest8=replace(destination,user_id=8,sender_ref='user:8')
        grant=ShareGrant(8,asset['id'],1,0,rev['sha256'],dest8.scope.key,'manual:'+preview['id'],datetime.now(timezone.utc)+timedelta(minutes=10))
        copy=await share_copy(self.service,private,dest8,grant)
        await self.projects.status(self.project.id,self.actor,1,'deleted')
        self.assertEqual(copy['id'],(await self.repo.read(copy['id'],dest8))[0]['id'])

    async def test_telegram_publication_preview_full_file_and_membership_recheck(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,patch
        from bot.project_commands import project_command
        message=SimpleNamespace(text='/project publish_preview -101 5 version=1',message_id=100,chat_id=-100,reply_text=AsyncMock(),reply_document=AsyncMock())
        member=SimpleNamespace(status='member')
        bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=-101,type='supergroup',is_forum=True)),get_chat_member=AsyncMock(return_value=member))
        with patch('bot.project_commands.enabled',return_value=True),patch('bot.project_commands.actor_for_current',new=AsyncMock(return_value=self.actor)),patch('bot.project_commands.service_for_bot',new=AsyncMock(return_value=self.service)):
            await project_command(SimpleNamespace(effective_message=message),SimpleNamespace(bot=bot))
            payload=json.loads(message.reply_document.call_args.args[0].getvalue())
            self.assertEqual(-101,payload['destination']['chat_id']); self.assertEqual('Read invoices',payload['goal'])
            message.text='/project publish '+payload['id']; member.status='left'
            await project_command(SimpleNamespace(effective_message=message),SimpleNamespace(bot=bot))
            self.assertIn('Не удалось',message.reply_text.call_args.args[0])
            async with self.pool.acquire() as conn: self.assertEqual(1,await conn.fetchval('SELECT COUNT(*) FROM arti_projects'))
            member.status='member'; await project_command(SimpleNamespace(effective_message=message),SimpleNamespace(bot=bot))
            self.assertIn('Проект создан',message.reply_text.call_args.args[0])
            bot.get_chat_member.assert_awaited_with(-101,7)
