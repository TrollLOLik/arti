"""Synthetic database connections; no environment keys or provider network."""
from contextlib import contextmanager, asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from ai.capabilities import CapabilityRegistry, ModelEndpoint
from ai.providers.connections import ModelConnection, ModelRoutes


def model_row(key='fixture', model=None, **fields):
    row = dict(key=key, name=key, model=model or key, provider='Fixture',
        base_url='https://fixture.invalid/v1', api_key='fixture-key',
        is_active=True, is_maintenance=False, request_options={}, fast_request_options={},
        timeout_seconds=60, capabilities={'inputs': ['text'], 'features': ['structured_output']})
    row.update(fields)
    return row


def connection(key='fixture', model=None, **fields):
    row = model_row(key, model)
    row.update(fields)
    return ModelConnection.from_row(row)


@contextmanager
def configured_models(*rows):
    async def resolve(selection):
        exact = next((row for row in rows if row['key'] == selection), None)
        matches = [row for row in rows if row['model'] == selection]
        if exact:
            return exact
        if len(matches) > 1:
            raise ValueError('ambiguous_model_selection')
        return matches[0] if matches else None
    with patch('database.models.AIModel.get_connection', side_effect=resolve), \
         patch('database.models.AIModel.get_available_connections', AsyncMock(return_value=list(rows))):
        yield


@contextmanager
def google_generation(provider):
    """Exercise the native grounding adapter without real clients or DB calls."""
    selected = connection('gemini', 'gemini-2.5-flash',
        base_url='https://generativelanguage.googleapis.com/v1beta/openai',
        capabilities={'inputs': ['text', 'image', 'video'], 'features': ['search', 'maps']})
    endpoint = next(c for c in selected.endpoints() if c.provider == 'gemini')
    routes = ModelRoutes(selected, CapabilityRegistry([endpoint]), {'gemini': selected})
    google = NS(aio=NS(models=NS(generate_content=provider), aclose=AsyncMock()), close=Mock())
    @asynccontextmanager
    async def client_context(_connection):
        try:
            yield google
        finally:
            await google.aio.aclose()
            google.close()
    with patch('ai.generation.load_model_routes', AsyncMock(return_value=routes)), \
         patch('ai.generation.google_client_for', side_effect=client_context):
        yield google
