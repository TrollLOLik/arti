"""Native Google SDK transport with database credentials and bounded ownership."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from google import genai
from google.genai import types

from ai.providers.connections import provider_http_client
from materials.types import MaterialError


@asynccontextmanager
async def google_client_for(connection, *, timeout=None, http_client=None):
    # Legacy OpenAI Google records may still use the native grounding adapter.
    base_url = connection.base_url.removesuffix('/v1beta/openai').removesuffix('/v1beta')
    http = http_client if http_client is not None else provider_http_client()
    deadline = min(timeout or connection.timeout_seconds, connection.timeout_seconds)
    client = None
    try:
        client = genai.Client(api_key=connection.api_key, vertexai=False,
            http_options=types.HttpOptions(base_url=base_url, api_version='v1beta',
                # Google rejects x-server-timeout below 10 s. The local async
                # deadline remains independent and can cancel much sooner.
                timeout=max(10000, int(deadline * 1000)),
                httpx_async_client=http, client_args={'trust_env': False, 'follow_redirects': False},
                async_client_args={'trust_env': False, 'follow_redirects': False},
                retry_options=types.HttpRetryOptions(attempts=1)))
        async with asyncio.timeout(deadline):
            yield client
    finally:
        try:
            if client is not None:
                try:
                    await client.aio.aclose()
                finally:
                    client.close()
        finally:
            if http_client is None:
                await http.aclose()


def google_config(connection, tokens, temperature=0.7, *, system='', structured=False, fast=False, **fields):
    options = {**connection.request_options, **(connection.fast_request_options if fast else {})}
    # Legacy grounding records carry OpenAI options; never send them to genai.
    if connection.api_type != 'gemini':
        options = {k: v for k, v in options.items() if k in ('temperature', 'top_p', 'seed')}
    actual_temperature = options.pop('temperature', temperature)
    if actual_temperature is not None:
        options['temperature'] = actual_temperature
    thinking = {**options.pop('thinking_config', {}), 'include_thoughts': False}
    options['thinking_config'] = types.ThinkingConfig(**thinking)
    if structured and 'structured_output' in connection.capabilities.get('features', []):
        options['response_mime_type'] = 'application/json'
    return types.GenerateContentConfig(max_output_tokens=tokens, system_instruction=system or None,
        **options, **fields)


def google_messages(messages):
    system, contents = [], []
    for message in messages:
        role, content = message.get('role'), message.get('content')
        if not isinstance(content, str):
            raise MaterialError('invalid_structured_messages')
        if role in ('system', 'developer'):
            system.append(content)
        elif role in ('user', 'assistant'):
            contents.append(types.Content(role='model' if role == 'assistant' else 'user',
                parts=[types.Part.from_text(text=content)]))
        else:
            raise MaterialError('invalid_structured_messages')
    if not contents:
        raise MaterialError('empty_structured_messages')
    return '\n\n'.join(system), contents


async def google_complete(connection, messages, tokens, temperature, *, fast=False, timeout=None, http_client=None):
    system, contents = google_messages(messages)
    config = google_config(connection, tokens, temperature, system=system, structured=True, fast=fast)
    try:
        async with google_client_for(connection, timeout=timeout, http_client=http_client) as client:
            response = await client.aio.models.generate_content(model=connection.model, contents=contents, config=config)
    except genai.errors.APIError as exc:
        status = exc.code if isinstance(exc.code, int) and 400 <= exc.code <= 599 else 502
        # Provider errors can contain prompts or secrets. Return a stable code.
        return httpx.Response(status, json={'error': {'code': 'google_api_error'}})
    finish = str(response.candidates[0].finish_reason) if response.candidates else ''
    usage = response.usage_metadata
    return httpx.Response(200, json={'model': connection.model, 'choices': [{
        'message': {'role': 'assistant', 'content': response.text or ''},
        'finish_reason': 'length' if finish.endswith('MAX_TOKENS') else 'stop'}],
        'usage': {'prompt_tokens': (usage.prompt_token_count or 0) if usage else 0,
            'completion_tokens': ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0,
            'total_tokens': (usage.total_token_count or 0) if usage else 0}})


@asynccontextmanager
async def google_video(client, video):
    """Upload and delete a local video with the same credential as generation."""
    if not isinstance(video, (str, Path)):
        yield video
        return
    path = Path(video)
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise MaterialError('video_byte_limit')
    uploaded = None
    try:
        uploaded = await client.aio.files.upload(file=path, config=types.UploadFileConfig(mime_type='video/mp4'))
        async with asyncio.timeout(90):
            while uploaded.state and uploaded.state.name == 'PROCESSING':
                from materials.runtime import guard_current
                await guard_current()
                await asyncio.sleep(2)
                uploaded = await asyncio.wait_for(client.aio.files.get(name=uploaded.name), 15)
        if not uploaded.state or uploaded.state.name != 'ACTIVE':
            raise MaterialError('video_processing_failed')
        yield uploaded
    finally:
        if uploaded is not None and uploaded.name:
            try:
                await asyncio.wait_for(client.aio.files.delete(name=uploaded.name), 5)
            except Exception:
                pass  # A cleanup failure must not replace the response/cancellation.
