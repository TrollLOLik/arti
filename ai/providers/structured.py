"""Bounded JSON calls through the same model selection as chat responses."""
import asyncio
import os

import httpx


class SelectedModelClient:
    def __init__(self, *, resolver=None, client=None, google_client=None, timeout=90, fast=False):
        self.resolver = resolver
        self.client = client
        self.google_client = google_client
        self.timeout = timeout
        self.owned = client is None
        self.fast = fast

    async def model_for(self, chat_id):
        if self.resolver is None:
            from utils.model_selection import get_chat_model
            model = await get_chat_model(chat_id)
        else:
            model = await self.resolver(chat_id)
        if not isinstance(model, str) or not model.strip():
            raise ValueError('invalid_selected_model')
        return model

    async def close(self):
        if self.owned and self.client is not None:
            await self.client.aclose()

    async def complete(self, model, messages, tokens, temperature=0):
        # The caller pins selection once for a bounded retry sequence. A model
        # changed in another chat cannot change this request or its retries.
        async with asyncio.timeout(self.timeout):
            if model.startswith('gemini'):
                from google.genai import types
                client = self.google_client
                if client is None:
                    from config import genai_client
                    client = genai_client
                system = '\n\n'.join(m['content'] for m in messages if m['role'] == 'system')
                contents = [types.Content(role='model' if m['role'] == 'assistant' else 'user',
                    parts=[types.Part(text=m['content'])]) for m in messages if m['role'] != 'system']
                thinking = None
                if self.fast:
                    if model.startswith(('gemini-3.1-flash-lite','gemini-3-flash')):
                        thinking = types.ThinkingConfig(thinking_level='minimal')
                    elif model.startswith('gemini-2.5-flash'):
                        thinking = types.ThinkingConfig(thinking_budget=0)
                try:
                    result = await client.aio.models.generate_content(model=model, contents=contents,
                        config=types.GenerateContentConfig(system_instruction=system, temperature=temperature,
                            max_output_tokens=tokens, response_mime_type='application/json',thinking_config=thinking))
                except (asyncio.CancelledError, httpx.TimeoutException):
                    raise
                except Exception as exc:
                    status = getattr(exc, 'code', None) or getattr(exc, 'status_code', None)
                    return httpx.Response(status if isinstance(status, int) and 400 <= status <= 599 else 503,
                        json={'error': {'code': 'structured_provider_failed'}})
                try:
                    text = result.text
                except (ValueError, AttributeError):
                    text = None
                usage = getattr(result, 'usage_metadata', None)
                candidates = getattr(result, 'candidates', None) or []
                finish = str(getattr(candidates[0], 'finish_reason', '')) if candidates else ''
                return httpx.Response(200, json={'choices': [{'message': {'content': text},
                    'finish_reason': 'length' if 'MAX_TOKENS' in finish else 'stop'}],
                    'usage': {'prompt_tokens': getattr(usage, 'prompt_token_count', 0),
                        'completion_tokens': getattr(usage, 'candidates_token_count', 0)}})
            from config import OMNIROUTE_BASE_URL
            if not OMNIROUTE_BASE_URL:
                return httpx.Response(503, json={'error': {'code': 'missing_chat_endpoint'}})
            if self.client is None:
                self.client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
            return await self.client.post(OMNIROUTE_BASE_URL.rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + os.getenv('OMNIROUTE_API_KEY', '')},
                json={'model': model, 'messages': messages, 'temperature': temperature,
                    'max_tokens': tokens, 'response_format': {'type': 'json_object'}})
