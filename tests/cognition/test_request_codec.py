"""Offline codec contracts: no providers, production database or filesystem uploads."""
import io
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot.request_codec import encode_value, decode_value, encode_request, decode_request, CodecError
from cognition.scope import TransportScope
from cognition.types import ContextKey, ExpressionPlan


class CodecTests(unittest.IsolatedAsyncioTestCase):
    async def roundtrip(self,value):
        return await decode_value(await encode_value(value))

    async def test_nested_json_tuple_and_reserved_keys(self):
        data={'codec':999,'kind':'malicious','values':(True,None,1,1.5,['text'])}
        self.assertEqual(await self.roundtrip(data),data)

    async def test_bytes_stream_and_cursor(self):
        raw=b'\x00\xffbinary'; stream=io.BytesIO(raw); stream.name='/private/name.wav'; stream.seek(3)
        encoded=await encode_value(stream)
        self.assertEqual(stream.tell(),3)
        restored=await decode_value(encoded)
        self.assertEqual(restored.read(),raw); self.assertEqual(restored.name,'name.wav')
        self.assertNotIn('/private',str(encoded))
        self.assertEqual(await self.roundtrip(raw),raw)

    async def test_rejects_unknown_objects_paths_and_bad_versions(self):
        for obj in (object(),Path('/tmp/private'),float('nan'),{1:'value'}):
            with self.assertRaises(CodecError): await encode_value(obj)
        with self.assertRaises(CodecError): await decode_value({'codec':999,'kind':'dict','items':{}})
        with self.assertRaises(CodecError): await decode_value({'codec':1,'kind':'bytes','data':'!','filename':None})
        with self.assertRaises(CodecError): await decode_value({'codec':1,'kind':'__import__','value':None})

    async def test_binary_and_aggregate_limits(self):
        with patch('bot.request_codec.MAX_BINARY',2):
            with self.assertRaises(CodecError): await encode_value(b'123')
            with self.assertRaises(CodecError): await decode_value({'codec':1,'kind':'bytes','data':'MTIz','filename':None})
        with patch('bot.request_codec.MAX_PAYLOAD',10):
            with self.assertRaises(CodecError): await encode_value('x'*11)

    async def test_telegram_media_and_receipt(self):
        from telegram import InputMediaPhoto, InputFile, Message, Chat, ReplyParameters
        media=InputMediaPhoto(InputFile(b'photo',filename='x.png'),caption='caption')
        result=await self.roundtrip(media)
        self.assertEqual(result.media.input_file_content,b'photo')
        self.assertEqual(result.caption,'caption')
        reply=await self.roundtrip(ReplyParameters(message_id=12))
        self.assertEqual(reply.message_id,12)
        receipt=Message(7,datetime.now(timezone.utc),Chat(1,'private'),text='sent')
        out=await self.roundtrip([receipt,True])
        self.assertEqual(out[0].message_id,7); self.assertEqual(out[0].chat.id,1)
        self.assertEqual(out[1],True)

    async def test_scope_and_context(self):
        for value in (TransportScope(1,user_id=2),ContextKey('arti',1)):
            self.assertEqual(await self.roundtrip(value),value)

    async def test_request_strips_clients_and_checks_destination(self):
        bot=object(); request=dict(type='text',chat_id=1,user_id=2,user_message='hello',
            message_id=3,context=SimpleNamespace(bot=bot),bot=bot,
            _telegram_scope=TransportScope(1,user_id=2),enqueued_at=123.)
        encoded=await encode_request(request); result=await decode_request(encoded,bot)
        self.assertIs(result['context'].bot,bot); self.assertNotIn('enqueued_at',result)
        request['_telegram_scope']=TransportScope(2)
        with self.assertRaises(CodecError): await decode_request(await encode_request(request),bot)
        request['api_key']='secret'
        with self.assertRaises(CodecError): await encode_request(request)

    async def test_unsupported_disk_backed_task_fails_closed(self):
        with self.assertRaises(CodecError): await encode_request(dict(type='vclone',reference_path='/tmp/audio'))

    async def test_turn_recovers_current_source_and_rejects_epoch(self):
        from cognition.runtime import PreparedTurn
        from cognition.types import CognitiveEvent, EvidenceRef, Origin, AudienceScope
        from cognition.serialization import dump
        from cognition.affect import expression, initial_state
        from cognition.repositories import SuppressedEvidence
        from unittest.mock import MagicMock
        at=datetime.now(timezone.utc); context=ContextKey('arti',1)
        # Use the repository's ordinary event fixture to keep its contract exact.
        from tests.cognition.test_affect import event
        ev=event('fixture',text='current source')
        row={'payload':dump(ev),'suppression_epoch':3,'authority':'active','rebuilding':False}
        conn=SimpleNamespace(fetchrow=AsyncMock(return_value=row),fetchval=AsyncMock(return_value=1))
        acquired=MagicMock(); acquired.__aenter__=AsyncMock(return_value=conn); acquired.__aexit__=AsyncMock(return_value=False)
        runtime=SimpleNamespace(pool=SimpleNamespace(acquire=lambda:acquired),_validate_current_scene=AsyncMock())
        turn=PreparedTurn(runtime,1,2,ev,expression(initial_state(ev.context,at)),'remembered',3,'active')
        turn.supporting_event_ids=[2]; turn.send_ordinal=4
        turn.retrieval_diagnostics={'status':'incomplete','indexed_chunks':8,'total_chunks':48}
        turn.private_memory_ids=[7,9]
        wire=await encode_value(turn)
        self.assertNotIn('current source',str(wire))
        with patch('cognition.runtime.get_runtime',return_value=runtime):
            restored=await decode_value(wire)
            self.assertEqual(restored.event.text,'current source')
            self.assertEqual(restored.send_ordinal,4); self.assertEqual(restored.supporting_event_ids,[2])
            self.assertEqual(restored.retrieval_diagnostics,turn.retrieval_diagnostics)
            self.assertEqual(restored.private_memory_ids,[7,9])
            row['suppression_epoch']=4
            with self.assertRaises(SuppressedEvidence): await decode_value(wire)
            row['suppression_epoch']=3; conn.fetchrow.return_value=None
            with self.assertRaises(SuppressedEvidence): await decode_value(wire)

    async def test_material_guard_is_revalidated(self):
        from materials.types import AccessContext, MaterialScope, MaterialError
        from materials.runtime import MaterialUse
        actor=AccessContext(MaterialScope('arti',1,-1,'private'),2,'user:2')
        service=SimpleNamespace(repository=SimpleNamespace(read=AsyncMock(return_value=({'generation':3,'current_version':1},None))))
        use=MaterialUse('asset',actor,1,3,service)
        wire=await encode_value(use)
        with patch('materials.runtime.service_for_bot',new=AsyncMock(return_value=service)):
            restored=await decode_value(wire)
            self.assertEqual(restored.asset_id,'asset'); service.repository.read.assert_awaited_once()
            service.repository.read.return_value=({'generation':4,'current_version':1},None)
            with self.assertRaises(MaterialError): await decode_value(wire)

    async def test_request_fence_prevents_replay_after_context_reset(self):
        from unittest.mock import MagicMock
        from cognition.repositories import SuppressedEvidence
        row={'id':1,'suppression_epoch':4,'rebuilding':False}
        conn=SimpleNamespace(fetchrow=AsyncMock(return_value=row),fetchval=AsyncMock(return_value=1))
        acquired=MagicMock(); acquired.__aenter__=AsyncMock(return_value=conn); acquired.__aexit__=AsyncMock(return_value=False)
        runtime=SimpleNamespace(pool=SimpleNamespace(acquire=lambda:acquired),_validate_current_scene=AsyncMock())
        request=dict(type='text',chat_id=10,_request_mode='default',_cognitive_context=ContextKey('arti',10),_cognitive_source_ids=[2])
        with patch('cognition.runtime.get_runtime',return_value=runtime):
            wire=await encode_request(request)
            self.assertEqual(wire['fence'],{'context_id':1,'epoch':4})
            result=await decode_request(wire,object()); self.assertEqual(result['_request_mode'],'default')
            row['suppression_epoch']=5
            with self.assertRaises(SuppressedEvidence): await decode_request(wire,object())
            row['suppression_epoch']=4; conn.fetchval.return_value=0
            with self.assertRaises(SuppressedEvidence): await decode_request(wire,object())

    async def test_all_guard_types_restore_live_repositories(self):
        from materials.types import AccessContext, MaterialScope
        from materials.runtime import ComputationUse, DerivativeUse
        from projects.types import ProjectUse, WorkflowUse
        actor=AccessContext(MaterialScope('arti',1,-1,'private'),2,'user:2')
        repository=SimpleNamespace(pool=object()); service=SimpleNamespace(repository=repository)
        guards=(ComputationUse('c',actor,repository),DerivativeUse('d',actor,repository,'transcript'),
            ProjectUse('p',actor,repository,2,3),WorkflowUse('w',actor,repository,3,'h','active'))
        for guard in guards:
            with self.subTest(kind=type(guard).__name__), patch('bot.request_codec._guard_material_ids',new=AsyncMock(return_value=['asset'])), patch('materials.runtime.service_for_bot',new=AsyncMock(return_value=service)),patch.object(type(guard),'validate',new=AsyncMock()) as check:
                restored=await self.roundtrip(guard)
                self.assertEqual(restored.actor,actor)
                self.assertIsNot(restored.repository,repository)
                check.assert_awaited_once()

    async def test_dependencies_only_read_known_records(self):
        from bot.request_codec import dependencies
        from materials.types import AccessContext, MaterialScope
        from materials.runtime import MaterialUse
        actor=AccessContext(MaterialScope('arti',1,-1,'private'),2,'user:2')
        guard=MaterialUse('asset',actor,1,1,object(),7,8)
        payload=await encode_request(dict(type='text',chat_id=1,user_id=2,_cognitive_source_ids=[5],_material_uses=(guard,)))
        payload['fence']={'context_id':7,'epoch':1}
        self.assertEqual(dependencies(payload),dict(context_ids=[7],source_event_ids=[5,8],material_ids=['asset']))
        fake={'codec':1,'kind':'MaterialUse','value':{'codec':1,'kind':'dict','items':{'asset_id':'fake'}}}
        self.assertEqual(dependencies(await encode_value(fake)),dict(context_ids=[],source_event_ids=[],material_ids=[]))

    async def test_coalesce_preserves_scope_media_and_provenance(self):
        from bot.request_codec import coalesce_requests,dependencies
        from dataclasses import replace
        from materials.types import AccessContext,MaterialScope
        from materials.runtime import MaterialUse
        actor=AccessContext(MaterialScope('arti',10,3,'supergroup'),2,'user:2')
        guard=MaterialUse('asset',actor,1,1,object(),7,8)
        a=dict(type='text',chat_id=10,user_id=2,message_id=1,user_message='first',
               _telegram_scope=TransportScope(10,3,'supergroup',2,1),_cognitive_source_ids=[1],
               base64_image='first image',document_text='first document',_material_uses=(guard,))
        b={**a,'message_id':2,'user_message':'second','_cognitive_source_ids':[2],
            'base64_image':'second image','document_text':'second document',
            '_telegram_scope':replace(a['_telegram_scope'],message_id=2)}
        old=await encode_request(a); new=await encode_request(b)
        merged=coalesce_requests(old,new); data=merged['value']['items']
        self.assertEqual(data['user_message'],'first\nsecond'); self.assertEqual(data['message_id'],2)
        self.assertEqual(data['base64_image'],'first image'); self.assertEqual(data['document_text'],'first document\nsecond document')
        self.assertEqual(data['_telegram_scope'],old['value']['items']['_telegram_scope'])
        self.assertEqual(len(data['_material_uses']['items']),1)
        self.assertEqual(dependencies(merged)['source_event_ids'],[1,2,8])
        self.assertEqual(old['value']['items']['user_message'],'first')
        for field,value in (('user_id',3),('_request_mode','rp'),('chat_id',20)):
            changed={**b,field:value}
            self.assertIsNone(coalesce_requests(old,await encode_request(changed)))
        changed={**b,'_telegram_scope':replace(b['_telegram_scope'],topic_id=4)}
        self.assertIsNone(coalesce_requests(old,await encode_request(changed)))
        new['fence']={'context_id':8,'epoch':1}
        self.assertIsNone(coalesce_requests(old,new))

    async def test_coalesce_rejects_bounds_and_nontext(self):
        from bot.request_codec import coalesce_requests
        a=await encode_request(dict(type='text',chat_id=1,user_id=2,user_message='x'*8000))
        b=await encode_request(dict(type='text',chat_id=1,user_id=2,user_message='x'))
        self.assertIsNone(coalesce_requests(a,b))
        media=await encode_request(dict(type='image',chat_id=1,user_id=2,prompt='x'))
        self.assertIsNone(coalesce_requests(a,media))

    async def test_indirect_guard_resolution_failure_aborts_encoding(self):
        from materials.types import AccessContext,MaterialScope
        from materials.runtime import DerivativeUse
        actor=AccessContext(MaterialScope('arti',1,-1,'private'),2,'user:2')
        guard=DerivativeUse('d',actor,object(),'material_review')
        with patch('bot.request_codec._guard_material_ids',new=AsyncMock(side_effect=RuntimeError('unavailable'))):
            with self.assertRaises(RuntimeError): await encode_value({'private':'body','guard':guard})
        with self.assertRaises(CodecError):
            await decode_value({'codec':1,'kind':'DerivativeUse','value':await encode_value({})})
