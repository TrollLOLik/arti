"""Endpoint-specific capability contracts; credentials never enter the registry."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    key: str | None = None

    def supports(self, request):
        age = datetime.now(timezone.utc) - self.observed_at if self.observed_at is not None else None
        fresh = age is None or -timedelta(minutes=5) <= age < timedelta(days=7)
        return self.available and fresh and request.inputs <= self.inputs and request.features <= self.features and len(request.prompt) + len(request.system) + sum(len(text) for _, text in getattr(request, 'history', ())) <= self.max_input_chars


@dataclass(frozen=True)
class RouteDecision:
    endpoint: ModelEndpoint
    requested_model: str
    reason: str


class CapabilityRegistry:
    def __init__(self, endpoints=()):
        self.endpoints = list(endpoints)

    def route(self, model, request, provider=None, endpoint=None, key=None):
        preferred = [c for c in self.endpoints if c.model == model and (provider is None or c.provider == provider) and (endpoint is None or c.endpoint == endpoint) and (key is None or c.key == key)]
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
