"""Endpoint-specific capability registry. Unknown proxy models are text-only."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from materials.types import MaterialError


@dataclass(frozen=True)
class ModelEndpoint:
    model: str
    provider: str
    endpoint: str
    inputs: frozenset[str] = frozenset({'text'})
    features: frozenset[str] = frozenset()
    evidence: str = 'configured'
    observed_at: datetime | None = None
    max_input_chars: int = 500_000
    available: bool = True

    def supports(self, request):
        fresh = self.observed_at is None or datetime.now(timezone.utc) - self.observed_at < timedelta(days=7)
        return self.available and fresh and request.inputs <= self.inputs and request.features <= self.features and len(request.prompt) + len(request.system) <= self.max_input_chars


@dataclass(frozen=True)
class RouteDecision:
    endpoint: ModelEndpoint
    requested_model: str
    reason: str


class CapabilityRegistry:
    def __init__(self, endpoints=()):
        self.endpoints = list(endpoints)

    def route(self, model, request, provider=None, endpoint=None):
        preferred = [c for c in self.endpoints if c.model == model and (provider is None or c.provider == provider) and (endpoint is None or c.endpoint == endpoint)]
        for candidate in preferred:
            if candidate.supports(request):
                return RouteDecision(candidate, model, 'selected_model_supports_request')
        for candidate in self.endpoints:
            if candidate not in preferred and candidate.supports(request):
                return RouteDecision(candidate, model, 'selected_endpoint_unavailable_or_unsupported')
        raise MaterialError('no_capable_endpoint')

    @classmethod
    def from_manifest(cls, value):
        if value.get('version') != 1:
            raise MaterialError('invalid_capability_manifest')
        endpoints = []
        for row in value.get('endpoints', []):
            if row['provider'] not in ('gemini', 'openai') or row.get('evidence') not in ('configured', 'metadata', 'probe'):
                raise MaterialError('invalid_endpoint')
            at = datetime.fromisoformat(row['observed_at']) if row.get('observed_at') else None
            if at is not None and at.tzinfo is None:
                raise MaterialError('naive_capability_clock')
            if row.get('evidence') != 'configured' and at is None:
                raise MaterialError('missing_capability_clock')
            endpoints.append(ModelEndpoint(**{**row, 'inputs': frozenset(row.get('inputs', ['text'])), 'features': frozenset(row.get('features', [])), 'observed_at': at}))
        return cls(endpoints)


def registry_for(model, proxy_endpoint):
    """Explicit operator config + conservative existing Google adapters.

    Manifest capabilities bind to an exact API URL. Metadata/probe evidence
    expires; a probe script can regenerate it without changing application code.
    """
    manifest_path = os.getenv('ARTI_CAPABILITY_MANIFEST', '').strip()
    registry = CapabilityRegistry.from_manifest(json.loads(Path(manifest_path).read_text(encoding='utf-8'))) if manifest_path else CapabilityRegistry()
    # A capability declaration is not permission to send prompts/API keys to an
    # arbitrary URL. Only the configured proxy and the Google adapter are usable.
    registry.endpoints = [c for c in registry.endpoints if c.provider == 'gemini' and c.endpoint == 'google-ai-studio'
        or c.provider == 'openai' and c.endpoint == proxy_endpoint]
    known_google = ('gemini-3.1-flash-lite-preview', 'gemini-3-flash-preview', 'gemini-2.5-flash')
    for name in known_google:
        if not any(c.model == name and c.provider == 'gemini' for c in registry.endpoints):
            registry.endpoints.append(ModelEndpoint(name, 'gemini', 'google-ai-studio', frozenset({'text', 'image', 'video'}),
                frozenset({'search', 'maps'}) if name == 'gemini-2.5-flash' else frozenset(), evidence='configured'))
    if not any(c.model == model for c in registry.endpoints):
        provider = 'gemini' if model.startswith('gemini') else 'openai'
        registry.endpoints.insert(0, ModelEndpoint(model, provider, 'google-ai-studio' if provider == 'gemini' else proxy_endpoint))
    return registry
