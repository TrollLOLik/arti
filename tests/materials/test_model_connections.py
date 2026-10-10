"""Real SDK/HTTP payloads with synthetic providers and disposable PostgreSQL."""
import asyncio
import base64
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from ai.providers.connections import ModelConnection, load_model_routes, resolve_connection
from ai.providers.contracts import GenerationRequest, ImageInput
from ai.providers.structured import SelectedModelClient
from database.models import AIModel, ChatModel
from materials.types import MaterialError
from tests.materials.fixtures import png
from tests.support.model_connections import configured_models, connection, model_row


def response(content='Synthetic reply'):
    return httpx.Response(200, json={'id':'fixture', 'object':'chat.completion', 'created':1,
        'model':'upstream', 'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}]})


class ConnectionTests(unittest.TestCase):
    def test_no_credentials_in_repr(self):
        selected=connection(api_key='PRIVATE_CREDENTIAL')
        self.assertNotIn('PRIVATE_CREDENTIAL',repr(selected))
        self.assertNotIn('https://',repr(selected))

    def test_invalid_destinations_and_options_fail_closed(self):
        for value in ('', 'https://user:secret@example.org/v1', 'https://example.org/v1?key=secret',
            'https://example.org/v1#fragment', 'http://example.org/v1', 'file:///tmp/v1',
            'https://example.org/v1/chat/completions', 'https://example.org:99999/v1'):
            with self.subTest(value=value),self.assertRaises(MaterialError): connection(base_url=value)
        for value in ({'model':'override'},{'extra_body':{'messages':[]}},{'token_parameter':'unsupported'},
            {'extra_body':{'api_key':'secret'}},{'temperature':float('nan')}):
            with self.subTest(value=value),self.assertRaises(MaterialError): connection(request_options=value)

    def test_null_credential_is_unconfigured_and_local_anonymous_is_explicit(self):
        with self.assertRaisesRegex(MaterialError,'not_configured'): connection(api_key=None)
        self.assertEqual(connection(base_url='http://127.0.0.1:1234/v1',api_key='').api_key,'')

    def test_option_defaults_fast_overrides_and_token_parameter(self):
        selected=connection(request_options={'token_parameter':'max_completion_tokens','temperature':None,
            'reasoning_effort':'high','extra_body':{'thinking':{'type':'disabled'}}},
            fast_request_options={'reasoning_effort':'low'})
        payload=selected.payload([{'role':'user','content':'fixture'}],120,structured=True)
        self.assertEqual(payload['max_completion_tokens'],120)
        self.assertNotIn('temperature',payload); self.assertNotIn('max_tokens',payload)
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        self.assertEqual(payload['reasoning_effort'],'high')
        self.assertEqual(selected.payload([],16,fast=True)['reasoning_effort'],'low')
        self.assertEqual(selected.payload([],16)['reasoning_effort'],'high')

    def test_json_format_can_be_disabled_without_losing_local_validation(self):
        self.assertNotIn('response_format',connection(capabilities={'inputs':['text'],'features':[]}).payload([],16,structured=True))

    def test_google_native_features_require_official_connection(self):
        caps={'inputs':['text','image','video'],'features':['search','maps']}
        for url in ('https://google.example/v1','https://generativelanguage.googleapis.com.evil.invalid/v1beta/openai'):
            with self.assertRaises(MaterialError): connection('gemini','gemini-2.5-flash',base_url=url,capabilities=caps)
        selected=connection('gemini','gemini-2.5-flash',base_url='https://generativelanguage.googleapis.com/v1beta/openai/',capabilities=caps)
        endpoints=selected.endpoints()
        self.assertEqual([x.provider for x in endpoints],['openai','gemini'])
        self.assertNotIn('search',endpoints[0].features)
        self.assertIn('search',endpoints[1].features)


class DirectRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_model_name_uses_independent_urls_and_credentials(self):
        rows=[model_row('first','same-model',base_url='https://first.invalid/v1',api_key='key-one'),
            model_row('second','same-model',base_url='https://second.invalid/api/v1',api_key='key-two')]
        captured=[]
        def handler(request):
            captured.append((str(request.url),request.headers.get('authorization'),json.loads(request.content)))
            return response('{}')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, configured_async(rows):
            client=SelectedModelClient(client=http)
            await asyncio.gather(*(client.complete(row['key'],[{'role':'user','content':'JSON'}],128) for row in rows))
        self.assertEqual({(url,key) for url,key,_ in captured},
            {('https://first.invalid/v1/chat/completions','Bearer key-one'),('https://second.invalid/api/v1/chat/completions','Bearer key-two')})
        self.assertTrue(all(payload['model']=='same-model' for _,_,payload in captured))
        with configured_models(*rows),self.assertRaisesRegex(MaterialError,'ambiguous'):
            await resolve_connection('same-model')

    async def test_gemini_name_uses_openai_payload_and_closes_client(self):
        from ai import generation
        captured=[]
        def handler(request): captured.append(request); return response()
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
        row=model_row('gemini','gemini-direct',api_key='DB_ONLY_KEY')
        with configured_models(row),patch.object(generation,'provider_http_client',return_value=http), \
             patch.dict(os.environ,{'OMNIROUTE_API_KEY':'WRONG_ENV_KEY'}):
            result=await generation.generate_response_stream(1,'Hello','User','',request_intent={},custom_system_prompt='System')
        self.assertEqual(result[0],'Synthetic reply')
        self.assertTrue(http.is_closed)
        self.assertEqual(captured[0].headers['authorization'],'Bearer DB_ONLY_KEY')
        self.assertEqual(json.loads(captured[0].content)['model'],'gemini-direct')

    async def test_image_fallback_receives_its_own_credential(self):
        from ai import generation
        rows=[model_row('text','text-model'),model_row('vision','vision-model',
            base_url='https://vision.invalid/v1',api_key='VISION_KEY',capabilities={'inputs':['text','image'],'features':[]})]
        captured=[]
        def handler(request): captured.append(request); return response('Image seen')
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with configured_models(*rows),patch.object(generation,'provider_http_client',return_value=http):
            result=await generation.generate_response_stream(1,'Describe','User','',model='text',request_intent={},
                base64_image=base64.b64encode(png()).decode(),custom_system_prompt='System')
        self.assertEqual(result[0],'Image seen')
        self.assertEqual(captured[0].headers['authorization'],'Bearer VISION_KEY')
        self.assertEqual(str(captured[0].url),'https://vision.invalid/v1/chat/completions')
        self.assertEqual(json.loads(captured[0].content)['messages'][1]['content'][1]['type'],'image_url')

    async def test_native_google_grounding_also_uses_database_credential(self):
        from ai import generation
        captured=[]
        def handler(request):
            captured.append(request)
            return httpx.Response(200,json={'candidates':[{'content':{'role':'model','parts':[{'text':'Grounded reply'}]},'finishReason':'STOP'}]})
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        row=model_row('gemini','gemini-2.5-flash',api_key='DATABASE_GOOGLE_KEY',
            base_url='https://generativelanguage.googleapis.com/v1beta/openai',
            capabilities={'inputs':['text','image'],'features':['search']})
        with configured_models(row),patch('ai.providers.google.provider_http_client',return_value=http), \
             patch.dict(os.environ,{'GEMINI_API_KEY':'WRONG_ENV_KEY','GOOGLE_GENAI_USE_VERTEXAI':'true'}):
            result=await generation.generate_response_stream(1,'Search','User','',request_intent={'web_search':True},custom_system_prompt='System')
        self.assertEqual(result[0],'Grounded reply'); self.assertTrue(http.is_closed)
        self.assertEqual(captured[0].headers['x-goog-api-key'],'DATABASE_GOOGLE_KEY')
        self.assertTrue(str(captured[0].url).startswith('https://generativelanguage.googleapis.com/'))
        self.assertIn('tools',json.loads(captured[0].content))

    async def test_anonymous_sdk_connection_omits_authorization_header(self):
        from ai import generation
        captured=[]
        def handler(request): captured.append(request); return response()
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with configured_models(model_row('gemini',api_key='')),patch.object(generation,'provider_http_client',return_value=http):
            await generation.generate_response_stream(1,'Hello','User','',request_intent={},custom_system_prompt='System')
        self.assertNotIn('authorization',captured[0].headers)

    async def test_provider_error_is_redacted_and_sdk_is_closed(self):
        from ai import generation
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401,json={'error':{'message':'PRIVATE_CREDENTIAL'}})))
        with configured_models(model_row('gemini')),patch.object(generation,'provider_http_client',return_value=http), \
             self.assertLogs('ai.generation',level='ERROR') as records:
            result=await generation.generate_response_stream(1,'Hello','User','',request_intent={},custom_system_prompt='System')
        self.assertTrue(generation.is_error_response(result[0])); self.assertTrue(http.is_closed)
        self.assertNotIn('PRIVATE_CREDENTIAL',' '.join(records.output))

    async def test_unconfigured_selected_model_never_uses_a_hidden_fallback(self):
        from ai import generation
        rows=[model_row('gemini',api_key=None),model_row('other')]
        with configured_models(*rows),patch.object(generation,'AsyncOpenAI') as sdk:
            result=await generation.generate_response_stream(1,'Hello','User','',request_intent={},custom_system_prompt='System')
        self.assertEqual(result[0],generation.ERROR_RESPONSE_NOT_CONFIGURED)
        self.assertTrue(generation.is_error_response(result[0])); sdk.assert_not_called()

    async def test_invalid_optional_model_cannot_break_selected_route(self):
        with configured_models(model_row('valid'),model_row('bad',base_url='invalid')):
            routes=await load_model_routes('valid')
        self.assertEqual(routes.route(GenerationRequest('Hello')).endpoint.key,'valid')

    async def test_structured_extra_body_merges_without_sdk_and_no_redirect(self):
        selected=connection(request_options={'extra_body':{'thinking':{'type':'disabled'}}})
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(307,headers={'location':'https://other.invalid/stolen'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True) as http:
            client=SelectedModelClient(client=http,connection_resolver=AsyncMock(return_value=selected))
            result=await client.complete('fixture',[{'role':'user','content':'JSON'}],16)
        self.assertEqual(result.status_code,307); self.assertEqual(len(requests),1)
        self.assertEqual(json.loads(requests[0].content)['thinking'],{'type':'disabled'})

    async def test_ping_skips_unconfigured_and_closes_on_error(self):
        from bot.commands import _ping_single_model
        with patch('ai.providers.structured.SelectedModelClient') as cls:
            self.assertEqual(await _ping_single_model({'key':'not-ready'},asyncio.Semaphore(1)),('not-ready','not_configured'))
            cls.assert_not_called()
            client=cls.return_value; client.complete=AsyncMock(side_effect=TimeoutError('secret'))
            client.close=AsyncMock()
            result=await _ping_single_model({'key':'ready','is_configured':True},asyncio.Semaphore(1))
        self.assertEqual(result,('ready','error')); client.close.assert_awaited_once()


class configured_async:
    """Reuse a synchronous patch context inside an async-with list."""
    def __init__(self,rows): self.context=configured_models(*rows)
    async def __aenter__(self): return self.context.__enter__()
    async def __aexit__(self,*args): return self.context.__exit__(*args)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class ConnectionSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        from utils.model_selection import _model_cache
        _model_cache.clear()

    async def asyncTearDown(self):
        from utils.model_selection import _model_cache
        _model_cache.clear(); await self.db.__aexit__(None,None,None)

    async def insert(self,key,model,**fields):
        async with self.pool.acquire() as conn:
            await conn.execute("""INSERT INTO ai_models(key,name,model,base_url,api_key)
                VALUES($1,$1,$2,'https://fixture.invalid/v1','PRIVATE_CREDENTIAL')""",key,model)

    async def test_migration_is_idempotent_preserves_catalog_and_rewrites_only_unique_ids(self):
        from database.ai_model_schema import migrate_ai_models
        await self.insert('stable','old-route'); await self.insert('duplicate-a','ambiguous'); await self.insert('duplicate-b','ambiguous')
        await self.insert('ambiguous','another-model')
        async with self.pool.acquire() as conn:
            await conn.executemany('INSERT INTO chat_models(chat_id,model_id) VALUES($1,$2)',
                [(1,'old-route'),(2,'ambiguous'),(3,'unknown-route')])
            await conn.execute('DROP TRIGGER IF EXISTS ai_model_capability_evidence ON ai_models')
            await conn.execute('ALTER TABLE ai_models DROP COLUMN base_url, DROP COLUMN api_key')
            await migrate_ai_models(conn); await migrate_ai_models(conn)
            rows=await conn.fetch('SELECT chat_id,model_id FROM chat_models ORDER BY chat_id')
            self.assertEqual([r['model_id'] for r in rows],['stable','ambiguous','unknown-route'])
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM ai_models'),4)
            self.assertEqual(await conn.fetchval("SELECT model FROM ai_models WHERE key='stable'"),'old-route')
            self.assertIsNone(await conn.fetchval("SELECT api_key FROM ai_models WHERE key='stable'"))

    async def test_public_catalog_never_fetches_private_fields(self):
        await self.insert('safe','actual-model')
        records=[await AIModel.get_by_key('safe'),await AIModel.get_by_model_id('actual-model')]
        records+=await AIModel.get_all_active()
        records+=(await AIModel.search_and_filter())[0]
        for record in records:
            self.assertTrue(record['is_configured'])
            self.assertNotIn('api_key',record); self.assertNotIn('base_url',record)
            self.assertNotIn('PRIVATE_CREDENTIAL',str(record))

    async def test_editing_endpoint_or_model_keeps_chat_choice_and_is_read_next_request(self):
        from utils.model_selection import set_chat_model,get_chat_model
        await self.insert('stable','old-model')
        await set_chat_model(42,'old-model')
        self.assertEqual(await get_chat_model(42),'stable')
        first=await resolve_connection(await get_chat_model(42))
        async with self.pool.acquire() as conn:
            await conn.execute("""UPDATE ai_models SET model='new-model',base_url='https://changed.invalid/v1',
                api_key='NEW_PRIVATE_CREDENTIAL' WHERE key='stable'""")
        current=await resolve_connection(await get_chat_model(42))
        self.assertEqual(await ChatModel.get(42,'other'),'stable')
        self.assertEqual(first.model,'old-model'); self.assertEqual(current.model,'new-model')
        self.assertEqual(current.api_key,'NEW_PRIVATE_CREDENTIAL')
        self.assertEqual(current.base_url,'https://changed.invalid/v1')

    async def test_disabled_and_maintenance_connections_are_not_routed(self):
        await self.insert('disabled','a'); await self.insert('maintenance','b')
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE ai_models SET is_active=FALSE WHERE key='disabled'")
            await conn.execute("UPDATE ai_models SET is_maintenance=TRUE WHERE key='maintenance'")
        self.assertEqual(await AIModel.get_available_connections(),[])
        for key in ('disabled','maintenance'):
            with self.assertRaisesRegex(MaterialError,'model_unavailable'): await resolve_connection(key)

    async def test_endpoint_change_invalidates_old_probe_but_preserves_explicit_new_evidence(self):
        from datetime import datetime,timezone,timedelta
        await self.insert('stable','model')
        at=datetime.now(timezone.utc)
        caps={'inputs':['text','image'],'features':[],'evidence':'probe','observed_at':at.isoformat()}
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE ai_models SET capabilities=$1::jsonb WHERE key=$2',json.dumps(caps),'stable')
            await conn.execute("UPDATE ai_models SET base_url='https://changed.invalid/v1' WHERE key='stable'")
            reset=json.loads(await conn.fetchval("SELECT capabilities FROM ai_models WHERE key='stable'"))
            self.assertEqual(reset,{'inputs':['text'],'features':[]})
            caps['observed_at']=(at+timedelta(seconds=1)).isoformat()
            await conn.execute("UPDATE ai_models SET model='new-model',capabilities=$1::jsonb WHERE key='stable'",json.dumps(caps))
            refreshed=json.loads(await conn.fetchval("SELECT capabilities FROM ai_models WHERE key='stable'"))
            self.assertEqual(refreshed,caps)

    async def test_native_gemini_cutover_imports_key_once_and_preserves_current_model(self):
        from tools.configure_model_connections import configure_native_gemini
        async with self.pool.acquire() as conn:
            await conn.execute("""INSERT INTO ai_models(key,name,model)
                VALUES('gemini','Gemini','gemini-3.5-flash-lite')""")
            await configure_native_gemini(conn,{'GEMINI_API_KEY':'EXISTING_PRIVATE_KEY'})
            await configure_native_gemini(conn,{'GEMINI_API_KEY':'DIFFERENT_ENV_KEY'})
            row=await AIModel.get_connection('gemini',conn=conn)
        self.assertEqual(row['api_type'],'gemini')
        self.assertEqual(row['model'],'gemini-3.5-flash-lite')
        self.assertEqual(row['api_key'],'EXISTING_PRIVATE_KEY')
        selected=ModelConnection.from_row(row)
        self.assertEqual(selected.endpoints()[0].provider,'gemini')
        self.assertIn('image',selected.endpoints()[0].inputs)
        self.assertTrue((await AIModel.get_by_key('gemini'))['is_configured'])

    async def test_native_protocol_change_invalidates_probe_and_blank_key_is_not_configured(self):
        from datetime import datetime,timezone
        await self.insert('stable','gemini-3.5-flash-lite')
        caps={'inputs':['text','image'],'features':[],'evidence':'probe',
            'observed_at':datetime.now(timezone.utc).isoformat()}
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE ai_models SET base_url='https://generativelanguage.googleapis.com', capabilities=$1::jsonb WHERE key='stable'",json.dumps(caps))
            await conn.execute("UPDATE ai_models SET api_type='gemini',api_key='' WHERE key='stable'")
            row=await AIModel.get_connection('stable',conn=conn)
        self.assertEqual(json.loads(row['capabilities']),{'inputs':['text'],'features':[]})
        self.assertFalse((await AIModel.get_by_key('stable'))['is_configured'])
        self.assertEqual(await AIModel.get_available_connections(),[])
