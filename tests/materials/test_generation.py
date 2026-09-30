import base64
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from ai.capabilities import CapabilityRegistry, ModelEndpoint
from tests.materials.fixtures import png


class GenerationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_proxy_vision_model_receives_png_content_block(self):
        from ai import generation
        from config import OMNIROUTE_BASE_URL
        create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='seen'))]))
        fake=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        registry=CapabilityRegistry([ModelEndpoint('qwen-vision','openai',OMNIROUTE_BASE_URL,frozenset({'text','image'}))])
        with patch.object(generation,'AsyncOpenAI',return_value=fake),patch.object(generation,'analyze_intent',new=AsyncMock(return_value={'web_search':False})),patch('ai.capabilities.registry_for',return_value=registry):
            output=await generation.generate_response_stream(1,'Describe this','User','',model='qwen-vision',base64_image=base64.b64encode(png()).decode(),custom_system_prompt='Be helpful.')
        self.assertEqual('seen',output[0])
        self.assertEqual('qwen-vision',create.call_args.kwargs['model'])
        image=create.call_args.kwargs['messages'][1]['content'][1]['image_url']['url']
        self.assertTrue(image.startswith('data:image/png;base64,'))

    async def test_picture_and_search_are_combined_in_google_payload(self):
        from ai import generation
        response=SimpleNamespace(text='grounded answer',candidates=[])
        analyze=AsyncMock(return_value={'web_search':True})
        with patch.object(generation,'analyze_intent',new=analyze),patch.object(generation.genai_client.models,'generate_content',return_value=response) as generate:
            result=await generation.generate_response_stream(1,'Find the current source of this chart','User','',base64_image=base64.b64encode(png()).decode(),custom_system_prompt='Be helpful.')
        self.assertEqual('grounded answer',result[0])
        analyze.assert_awaited_once()
        submitted=generate.call_args.kwargs
        self.assertEqual('gemini-2.5-flash',submitted['model'])
        self.assertEqual('image/png',submitted['contents'][0].inline_data.mime_type)
        self.assertIsNotNone(submitted['config'].tools[0].google_search)

    async def test_rp_does_not_turn_on_search_from_source_text(self):
        from ai import generation
        analyze=AsyncMock(return_value={'web_search':True})
        with patch.object(generation,'analyze_intent',new=analyze),patch.object(generation.genai_client.models,'generate_content',return_value=SimpleNamespace(text='story',candidates=[])):
            result=await generation.generate_response_stream(1,'Search the web','User','',is_rp_mode=True,custom_system_prompt='Tell a story.')
        self.assertEqual('story',result[0])
        analyze.assert_not_awaited()
