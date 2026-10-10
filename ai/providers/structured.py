"""Bounded JSON requests using the selected model's database connection."""
import asyncio

import httpx
from ai.providers.connections import resolve_connection
from ai.providers.contracts import GenerationRequest
from materials.types import MaterialError


class SelectedModelClient:
    def __init__(self, *, resolver=None, client=None, connection_resolver=None, timeout=90, fast=False):
        self.resolver = resolver
        self.client = client
        self.connection_resolver = connection_resolver or resolve_connection
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
            self.client = None

    async def complete(self, model, messages, tokens, temperature=0):
        # The caller pins selection once for a bounded retry sequence. A model
        # changed in another chat cannot change this request or its retries.
        async with asyncio.timeout(self.timeout):
            try:
                connection = await self.connection_resolver(model)
                request = GenerationRequest('\n'.join(message['content'] for message in messages),
                    structured_output='structured_output' in connection.capabilities.get('features', []))
                if not connection.endpoints()[0].supports(request):
                    raise MaterialError('model_capabilities_unavailable')
            except MaterialError as exc:
                return httpx.Response(503, json={'error': {'code': exc.code}})
            if connection.api_type == 'gemini':
                from ai.providers.google import google_complete
                return await google_complete(connection, messages, tokens, temperature, fast=self.fast,
                    timeout=self.timeout, http_client=self.client)
            payload = connection.payload(messages, tokens, temperature, structured=True, fast=self.fast)
            payload.update(payload.pop('extra_body', {}))
            if self.client is None:
                self.client = httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False)
            headers = {'Authorization': 'Bearer ' + connection.api_key} if connection.api_key else {}
            return await self.client.post(connection.base_url + '/chat/completions',
                headers=headers, json=payload, timeout=min(self.timeout, connection.timeout_seconds),
                follow_redirects=False)
