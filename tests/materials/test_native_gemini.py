"""Native Gemini regression coverage using the real SDK, no external requests."""
import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from google.genai import types

from ai.providers.connections import ModelConnection
from ai.providers.google import google_video
from ai.providers.structured import SelectedModelClient
from materials.types import MaterialError
from tests.materials.fixtures import png
from tests.support.model_connections import configured_models, model_row


def native_row(**fields):
    return model_row('gemini', 'gemini-3.5-flash-lite', api_type='gemini',
        base_url='https://generativelanguage.googleapis.com', api_key='DATABASE_GOOGLE_KEY',
        **fields)


def native_response(text='{"ok":true}', *, finish='STOP', thoughts=0):
    return httpx.Response(200, json={'candidates': [{'content': {'role': 'model',
        'parts': [{'text': 'Private reasoning', 'thought': True}, {'text': text}]},
        'finishReason': finish}], 'usageMetadata': {
            'promptTokenCount': 11, 'candidatesTokenCount': 5,
            'thoughtsTokenCount':thoughts, 'totalTokenCount': 16+thoughts}})


class NativeConnectionTests(unittest.TestCase):
    def test_native_protocol_is_explicit_and_rejects_wrong_endpoints_and_options(self):
        selected = ModelConnection.from_row(native_row())
        self.assertEqual([x.provider for x in selected.endpoints()], ['gemini'])
        for field, value in [('base_url','https://generativelanguage.googleapis.com/v1beta/openai'),
                ('base_url','https://generativelanguage.googleapis.com.evil.invalid'),
                ('api_key',''), ('api_type','unsupported'), ('request_options',{'extra_body':{'api_key':'secret'}}),
                ('request_options',{'thinking_config':{'include_thoughts':True}})]:
            row = native_row(); row[field] = value
            with self.subTest(field=field,value=value),self.assertRaises(MaterialError):
                ModelConnection.from_row(row)
        with self.assertRaises(MaterialError): selected.payload([],16)


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_sdk_messages_fast_options_usage_and_client_ownership(self):
        captured=[]
        def handler(request):
            captured.append(request)
            return native_response(thoughts=7)
        row=native_row(request_options={'thinking_config':{'thinking_level':'high'}},
            fast_request_options={'thinking_config':{'thinking_level':'minimal'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, configured_context(row):
            client=SelectedModelClient(client=http,fast=True)
            response=await client.complete('gemini',[
                {'role':'system','content':'JSON only'}, {'role':'user','content':'Earlier'},
                {'role':'assistant','content':'Prior reply'}, {'role':'user','content':'Now'}],64)
            await client.close()
            self.assertFalse(http.is_closed)
        payload=json.loads(captured[0].content)
        self.assertEqual(str(captured[0].url),
            'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent')
        self.assertEqual(captured[0].headers['x-goog-api-key'],'DATABASE_GOOGLE_KEY')
        self.assertEqual([c['role'] for c in payload['contents']],['user','model','user'])
        self.assertEqual(payload['systemInstruction']['parts'][0]['text'],'JSON only')
        thinking=payload['generationConfig']['thinkingConfig']
        self.assertEqual(thinking.get('thinkingLevel',thinking.get('thinking_level')).lower(),'minimal')
        self.assertFalse(thinking.get('includeThoughts',thinking.get('include_thoughts')))
        self.assertEqual(payload['generationConfig']['responseMimeType'],'application/json')
        body=response.json()
        self.assertEqual(body['choices'][0]['message']['content'],'{"ok":true}')
        self.assertEqual(body['usage']['total_tokens'],23)
        self.assertEqual(body['usage']['completion_tokens'],12)

    async def test_native_ordinary_reply_and_png_use_db_model_and_close_owned_transport(self):
        from ai import generation
        captured=[]
        def handler(request): captured.append(request); return native_response('Seen')
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        row=native_row(capabilities={'inputs':['text','image'],'features':[]})
        with configured_models(row),patch('ai.providers.google.provider_http_client',return_value=http), \
                patch.dict(os.environ,{'GEMINI_API_KEY':'WRONG_ENV_KEY','GOOGLE_GENAI_USE_VERTEXAI':'true'}), \
                patch.object(generation,'AsyncOpenAI') as openai:
            result=await generation.generate_response_stream(1,'Look','User','',
                base64_image=base64.b64encode(png()).decode(), request_intent={},custom_system_prompt='System')
        self.assertEqual(result[0],'Seen'); self.assertTrue(http.is_closed); openai.assert_not_called()
        payload=json.loads(captured[0].content)
        self.assertEqual(payload['contents'][0]['parts'][0]['inlineData']['mimeType'],'image/png')
        self.assertNotIn('tools',payload)
        self.assertNotIn('responseMimeType',payload['generationConfig'])

    async def test_native_keys_are_isolated_even_for_same_model(self):
        captured=[]
        def handler(request): captured.append(request.headers['x-goog-api-key']); return native_response()
        a=native_row(); b={**native_row(),'key':'second','api_key':'SECOND_KEY'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with configured_models(a,b):
                client=SelectedModelClient(client=http)
                await asyncio.gather(*(client.complete(key,[{'role':'user','content':'JSON'}],64)
                    for key in ('gemini','second')))
        self.assertEqual(set(captured),{'DATABASE_GOOGLE_KEY','SECOND_KEY'})

    async def test_native_provider_error_is_redacted_and_closed(self):
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(401,json={'error':{'code':401,'message':'DATABASE_GOOGLE_KEY secret prompt','status':'UNAUTHENTICATED'}})))
        with configured_models(native_row()),patch('ai.providers.google.provider_http_client',return_value=http):
            client=SelectedModelClient()
            result=await client.complete('gemini',[{'role':'user','content':'JSON'}],64)
        self.assertEqual(result.status_code,401); self.assertTrue(http.is_closed)
        self.assertEqual(result.json(),{'error':{'code':'google_api_error'}})

    async def test_cancellation_closes_native_sdk_transport(self):
        async def handler(request): await asyncio.sleep(20); return native_response()
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with configured_models(native_row()),patch('ai.providers.google.provider_http_client',return_value=http):
            client=SelectedModelClient(timeout=.05)
            with self.assertRaises(TimeoutError):
                await client.complete('gemini',[{'role':'user','content':'JSON'}],64)
        self.assertTrue(http.is_closed)

    async def test_ping_has_minimum_server_deadline_and_short_local_deadline(self):
        captured=[]
        def handler(request): captured.append(request); return native_response()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,configured_context(native_row()):
            result=await SelectedModelClient(client=http,timeout=5,fast=True).complete(
                'gemini',[{'role':'user','content':'Return JSON with value 1.'}],16)
        self.assertEqual(result.status_code,200)
        self.assertEqual(float(captured[0].headers['x-server-timeout']),10)

    async def test_native_visual_parser_and_sticker_batches(self):
        from ai.providers.visual import VisualAnalyzer
        from ai.stickers import _classify_via_gemini_vision
        captured=[]
        def handler(request):
            payload=json.loads(request.content); captured.append(payload)
            visual='Return factual' in str(payload.get('systemInstruction',''))
            return native_response(json.dumps({'summary':'Seen','objects':[],'relations':[],
                'limitations':[]} if visual else {'happy':[1]}))
        row=native_row(capabilities={'inputs':['text','image'],'features':['structured_output']})
        clients=[]
        def http_factory():
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)); clients.append(http); return http
        bot=NS(get_file=AsyncMock(return_value=NS(download_as_bytearray=AsyncMock(return_value=bytearray(png())))))
        with configured_models(row),patch('ai.providers.google.provider_http_client',side_effect=http_factory):
            result=await VisualAnalyzer('gemini').observe(png(),'image/png')
            stickers=[NS(file_id=str(i)) for i in range(15)]
            mappings=await _classify_via_gemini_vision(bot,'fixture',stickers)
        self.assertEqual(result['summary'],'Seen')
        self.assertEqual(mappings['happy'],['0','8'])
        self.assertEqual([sum('inlineData' in p for p in x['contents'][0]['parts']) for x in captured],[1,8,7])
        schema=captured[0]['generationConfig']['responseJsonSchema']
        self.assertEqual(schema['type'],'object')
        self.assertEqual(schema['properties']['objects']['items']['properties']['bbox']['items']['maximum'],1)
        self.assertEqual([x['generationConfig']['responseJsonSchema']['properties']['happy']['items']['maximum']
            for x in captured[1:]],[8,7])
        self.assertTrue(all(http.is_closed for http in clients))

    async def test_video_file_is_deleted_on_failure_and_cancel_with_same_client(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'clip.mp4'; path.write_bytes(b'synthetic video')
            for state in ('FAILED','PROCESSING','ACTIVE'):
                uploaded=types.File(name='files/private',uri='https://example.invalid/file',
                    mime_type='video/mp4',state=state)
                files=NS(upload=AsyncMock(return_value=uploaded),delete=AsyncMock(),get=AsyncMock())
                client=NS(aio=NS(files=files))
                if state=='FAILED':
                    with self.assertRaises(MaterialError):
                        async with google_video(client,path): pass
                elif state=='PROCESSING':
                    with self.assertRaises(TimeoutError):
                        async with asyncio.timeout(.03),google_video(client,path): pass
                else:
                    async with google_video(client,path) as video: self.assertIs(video,uploaded)
                files.upload.assert_awaited_once()
                files.delete.assert_awaited_once_with(name='files/private')


class configured_context:
    def __init__(self,row): self.context=configured_models(row)
    async def __aenter__(self): return self.context.__enter__()
    async def __aexit__(self,*args): return self.context.__exit__(*args)
