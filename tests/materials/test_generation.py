import base64
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from ai.capabilities import CapabilityRegistry, ModelEndpoint
from tests.materials.fixtures import png
from tests.support.model_connections import configured_models, model_row, google_generation


class GenerationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_proxy_vision_model_receives_png_content_block(self):
        from ai import generation
        create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='seen'))]))
        fake=SimpleNamespace(close=AsyncMock(),chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        row=model_row('qwen-vision',capabilities={'inputs':['text','image'],'features':[]})
        with patch.object(generation,'AsyncOpenAI',return_value=fake),patch.object(generation,'analyze_intent',new=AsyncMock(return_value={'web_search':False})),configured_models(row):
            output=await generation.generate_response_stream(1,'Describe this','User','',model='qwen-vision',base64_image=base64.b64encode(png()).decode(),custom_system_prompt='Be helpful.')
        self.assertEqual('seen',output[0])
        self.assertEqual('qwen-vision',create.call_args.kwargs['model'])
        image=create.call_args.kwargs['messages'][1]['content'][1]['image_url']['url']
        self.assertTrue(image.startswith('data:image/png;base64,'))

    async def test_picture_and_search_are_combined_in_google_payload(self):
        from ai import generation
        response=SimpleNamespace(text='grounded answer',candidates=[])
        analyze=AsyncMock(return_value={'web_search':True})
        generate=AsyncMock(return_value=response)
        with patch.object(generation,'analyze_intent',new=analyze),google_generation(generate):
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
        with patch.object(generation,'analyze_intent',new=analyze),google_generation(AsyncMock(return_value=SimpleNamespace(text='story',candidates=[]))):
            result=await generation.generate_response_stream(1,'Search the web','User','',is_rp_mode=True,custom_system_prompt='Tell a story.')
        self.assertEqual('story',result[0])
        analyze.assert_not_awaited()
