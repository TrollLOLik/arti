"""OpenAI-compatible and native Gemini connections read from PostgreSQL.

No environment proxy fallback and no connection cache: editing a DB row takes
effect on the next call. Public model metadata never carries these credentials.
"""
from dataclasses import dataclass, field
import ipaddress
import json
from urllib.parse import urlsplit, urlunsplit

from ai.capabilities import CapabilityRegistry, ModelEndpoint
from database.models import AIModel
from materials.types import MaterialError


def provider_http_client():
    import httpx
    return httpx.AsyncClient(trust_env=False, follow_redirects=False)


def _object(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('invalid_connection_options')
    # Reject NaN and oversized provider options before passing them to a client.
    if len(json.dumps(value, allow_nan=False)) > 16384:
        raise ValueError('connection_options_budget')
    return value


def _base_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing_model_endpoint')
    url = urlsplit(value.strip())
    if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('invalid_model_endpoint')
    url.port  # validate malformed/out-of-range ports
    if url.scheme == 'http':
        try:
            local = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            local = url.hostname.lower() == 'localhost'
        if not local:
            raise ValueError('model_endpoint_requires_https')
    path = url.path.rstrip('/')
    if path.endswith(('/chat/completions', '/responses')):
        raise ValueError('model_endpoint_requires_api_root')
    return urlunsplit((url.scheme, url.netloc, path, '', ''))


_OPTIONS = {'temperature', 'top_p', 'seed', 'reasoning_effort', 'verbosity',
    'presence_penalty', 'frequency_penalty', 'stop', 'extra_body', 'token_parameter'}
_RESERVED = {'model', 'messages', 'stream', 'tools', 'tool_choice', 'max_tokens',
    'max_completion_tokens', 'response_format', 'api_key', 'base_url', 'headers'}


_GOOGLE_OPTIONS = {'temperature', 'top_p', 'top_k', 'seed', 'presence_penalty',
    'frequency_penalty', 'stop_sequences', 'thinking_config'}


def _options(value, api_type='openai'):
    options = _object(value)
    if api_type == 'gemini':
        from google.genai import types
        if options.keys() - _GOOGLE_OPTIONS:
            raise ValueError('unsupported_google_request_options')
        thinking = options.get('thinking_config', {})
        if not isinstance(thinking, dict) or thinking.keys() - {'thinking_budget', 'thinking_level'}:
            raise ValueError('invalid_thinking_config')
        types.GenerateContentConfig(**options)
        return options
    if options.keys() - _OPTIONS:
        raise ValueError('unsupported_request_options')
    if options.get('token_parameter', 'max_tokens') not in ('max_tokens', 'max_completion_tokens'):
        raise ValueError('invalid_token_parameter')
    extra = options.get('extra_body', {})
    if not isinstance(extra, dict) or extra.keys() & _RESERVED:
        raise ValueError('reserved_request_options')
    return options


@dataclass(frozen=True)
class ModelConnection:
    key: str
    model: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    timeout_seconds: int = 60
    request_options: dict = field(default_factory=dict, repr=False)
    fast_request_options: dict = field(default_factory=dict, repr=False)
    capabilities: dict = field(default_factory=lambda: {'inputs': ['text'], 'features': []})
    api_type: str = 'openai'

    @classmethod
    def from_row(cls, row):
        if not row:
            raise MaterialError('model_connection_not_configured')
        if not row.get('is_active') or row.get('is_maintenance'):
            raise MaterialError('model_unavailable')
        if row.get('api_key') is None or not row.get('base_url'):
            raise MaterialError('model_connection_not_configured')
        try:
            key, model, api_key = row['key'], row['model'], row['api_key']
            api_type = row.get('api_type', 'openai')
            if api_type not in ('openai', 'gemini'):
                raise ValueError('unsupported_api_type')
            if not isinstance(model, str) or not model.strip() or not isinstance(api_key, str) or '\n' in api_key or '\r' in api_key:
                raise ValueError('invalid_model_connection')
            timeout = row.get('timeout_seconds', 60)
            if type(timeout) is not int or not 1 <= timeout <= 300:
                raise ValueError('invalid_model_timeout')
            connection = cls(key, model.strip(), _base_url(row['base_url']), api_key,
                timeout, _options(row.get('request_options', {}), api_type),
                _options(row.get('fast_request_options', {}), api_type),
                _object(row.get('capabilities', {'inputs': ['text'], 'features': []})), api_type)
            connection.endpoints()  # validate capabilities even for text-only calls
            return connection
        except (ValueError, TypeError, KeyError, MaterialError):
            raise MaterialError('invalid_model_connection') from None

    def endpoints(self):
        caps = self.capabilities
        allowed = {'inputs', 'features', 'evidence', 'observed_at', 'max_input_chars'}
        if caps.keys() - allowed or not isinstance(caps.get('inputs', ['text']), list) or not isinstance(caps.get('features', []), list):
            raise ValueError('invalid_model_capabilities')
        inputs, features = set(caps.get('inputs', ['text'])), set(caps.get('features', []))
        if 'text' not in inputs or inputs - {'text', 'image', 'video'} or features - {'search', 'maps', 'structured_output'}:
            raise ValueError('unsupported_model_capabilities')
        limit = caps.get('max_input_chars', 500000)
        if type(limit) is not int or not 1 <= limit <= 2000000:
            raise ValueError('invalid_model_input_limit')
        google = urlsplit(self.base_url)
        if self.api_type == 'gemini':
            if not self.api_key.strip() or google.scheme != 'https' or google.netloc != 'generativelanguage.googleapis.com' or google.path not in ('', '/v1beta'):
                raise ValueError('invalid_native_google_endpoint')
            row = {**caps, 'model': self.model, 'provider': 'gemini', 'endpoint': self.base_url,
                'key': self.key, 'inputs': sorted(inputs), 'features': sorted(features),
                'evidence': caps.get('evidence', 'configured')}
            return CapabilityRegistry.from_manifest({'version': 1, 'endpoints': [row]}).endpoints
        native_google = bool(self.api_key) and google.scheme == 'https' and google.netloc == 'generativelanguage.googleapis.com' and google.path == '/v1beta/openai' and self.model.startswith('gemini')
        if ('video' in inputs or features & {'search', 'maps'}) and not native_google:
            raise ValueError('native_features_require_google_endpoint')
        # JSON cannot supply an arbitrary endpoint or credential.
        row = {**caps, 'model': self.model, 'provider': 'openai', 'endpoint': self.base_url,
            'key': self.key, 'inputs': sorted(inputs - {'video'}),
            'features': sorted(features - {'search', 'maps'}), 'evidence': caps.get('evidence', 'configured')}
        endpoints = CapabilityRegistry.from_manifest({'version': 1, 'endpoints': [row]}).endpoints
        if native_google and ('video' in inputs or features & {'search', 'maps'}):
            native = {**row, 'provider': 'gemini', 'inputs': sorted(inputs), 'features': sorted(features)}
            endpoints += CapabilityRegistry.from_manifest({'version': 1, 'endpoints': [native]}).endpoints
        return endpoints

    def payload(self, messages, tokens, temperature=0.7, *, structured=False, fast=False):
        if self.api_type != 'openai':
            raise MaterialError('openai_payload_requires_openai_connection')
        options = {**self.request_options, **(self.fast_request_options if fast else {})}
        token_parameter = options.pop('token_parameter', 'max_tokens')
        extra = options.pop('extra_body', {})
        payload = dict(model=self.model, messages=messages, **{token_parameter: tokens})
        payload.update(options)
        # JSON null explicitly omits temperature for APIs that reject it.
        configured_temperature = payload.pop('temperature', temperature)
        if configured_temperature is not None:
            payload['temperature'] = configured_temperature
        if structured and 'structured_output' in self.capabilities.get('features', []):
            payload['response_format'] = {'type': 'json_object'}
        if extra:
            payload['extra_body'] = extra
        return payload


async def resolve_connection(selection, *, conn=None):
    try:
        row = await AIModel.get_connection(selection,conn=conn) if conn is not None else await AIModel.get_connection(selection)
    except ValueError:
        raise MaterialError('ambiguous_model_selection') from None
    return ModelConnection.from_row(row)


@dataclass
class ModelRoutes:
    selected: ModelConnection
    registry: CapabilityRegistry
    connections: dict = field(repr=False)

    def route(self, request):
        return self.registry.route(self.selected.model, request, provider=self.selected.api_type,
            endpoint=self.selected.base_url, key=self.selected.key)

    def connection_for(self, decision):
        return self.connections[decision.endpoint.key]


async def load_model_routes(selection):
    selected = await resolve_connection(selection)
    connections = {selected.key: selected}
    for row in await AIModel.get_available_connections():
        if row['key'] == selected.key:
            continue
        try:
            connection = ModelConnection.from_row(row)
            connections[connection.key] = connection
        except MaterialError:
            continue  # one invalid optional fallback cannot break the selected API
    endpoints = [endpoint for connection in connections.values() for endpoint in connection.endpoints()]
    return ModelRoutes(selected, CapabilityRegistry(endpoints), connections)
