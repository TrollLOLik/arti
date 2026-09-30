"""Versioned contracts; locators refer to originals, never guessed citations."""
from dataclasses import asdict, dataclass, field
from enum import Enum
from hashlib import sha256
import json
import math

CONTRACT_VERSION = 'materials-1'


class MaterialError(Exception):
    """Public errors carry stable codes, not provider text or file contents."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class MaterialScope:
    persona_id: str
    chat_id: int
    topic_id: int
    chat_type: str
    mode: str = 'default'
    scene_id: str = ''

    def __post_init__(self):
        if type(self.chat_id) is not int or type(self.topic_id) is not int or not -(2**63) <= self.chat_id < 2**63:
            raise MaterialError('invalid_scope_id')
        if not self.persona_id or self.chat_type not in ('private', 'group', 'supergroup'):
            raise MaterialError('unknown_scope')
        if self.mode not in ('default', 'rp') or (self.mode == 'rp' and not self.scene_id):
            raise MaterialError('unknown_scene')
        if self.chat_type != 'private' and self.topic_id < 0:
            raise MaterialError('unknown_topic')

    @classmethod
    def from_transport(cls, scope, mode='default', scene_id=''):
        if scope is None:
            raise MaterialError('missing_scope')
        return cls('arti', scope.chat_id, scope.topic_id, scope.chat_type, mode, scene_id)

    @property
    def key(self):
        return sha256(canonical(asdict(self)).encode()).hexdigest()

    @property
    def identity_key(self):
        return context_identity(self.persona_id, self.chat_id, self.topic_id, self.mode, self.scene_id)


@dataclass(frozen=True)
class AccessContext:
    scope: MaterialScope
    user_id: int | None
    sender_ref: str

    def __post_init__(self):
        if not self.sender_ref or (self.scope.chat_type == 'private' and self.user_id is None):
            raise MaterialError('unknown_owner')

    @property
    def realm(self):
        # Deduplication cannot reveal the existence of a private/other-topic file.
        visibility = self.user_id if self.scope.chat_type == 'private' else None
        return sha256(canonical([self.scope.key, visibility]).encode()).hexdigest()


class LocatorKind(str, Enum):
    DOCUMENT = 'document'
    PAGE = 'page'
    REGION = 'region'
    TIME = 'time'
    CELL = 'cell'
    PARAGRAPH = 'paragraph'


@dataclass(frozen=True)
class Locator:
    kind: LocatorKind
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    sheet: str | None = None
    cell: str | None = None
    paragraph: int | None = None

    def __post_init__(self):
        import re
        object.__setattr__(self, 'kind', LocatorKind(self.kind))
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            raise MaterialError('invalid_page')
        if self.kind == LocatorKind.PAGE and self.page is None:
            raise MaterialError('missing_page')
        if self.kind == LocatorKind.REGION:
            if self.bbox is None or len(self.bbox) != 4:
                raise MaterialError('missing_region')
            x0, y0, x1, y1 = self.bbox
            if not all(math.isfinite(v) for v in self.bbox) or not 0 <= x0 < x1 <= 1 or not 0 <= y0 < y1 <= 1:
                raise MaterialError('invalid_region')
        if self.kind == LocatorKind.TIME:
            if type(self.start_ms) is not int or type(self.end_ms) is not int or not 0 <= self.start_ms < self.end_ms:
                raise MaterialError('invalid_time')
        if self.kind == LocatorKind.CELL and (not self.sheet or not self.cell or not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]*(?::[A-Z]{1,3}[1-9][0-9]*)?', self.cell)):
            raise MaterialError('invalid_cell')
        if self.kind == LocatorKind.PARAGRAPH and (type(self.paragraph) is not int or self.paragraph < 1):
            raise MaterialError('invalid_paragraph')
        allowed = {
            LocatorKind.DOCUMENT: set(), LocatorKind.PAGE: {'page'},
            LocatorKind.REGION: {'page', 'bbox'}, LocatorKind.TIME: {'start_ms', 'end_ms'},
            LocatorKind.CELL: {'sheet', 'cell'}, LocatorKind.PARAGRAPH: {'paragraph'},
        }[self.kind]
        if any(v is not None and k not in allowed for k, v in asdict(self).items() if k != 'kind'):
            raise MaterialError('mixed_locator')

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, 'bbox': tuple(value['bbox']) if value.get('bbox') is not None else None})


@dataclass(frozen=True)
class ContentBlock:
    block_id: str
    kind: str
    locator: Locator
    text: str = ''
    parent_id: str | None = None
    ordinal: int = 0
    observation: str = 'extracted'
    quality: str = 'unassessed'
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in ('text', 'page', 'table', 'image', 'audio', 'video') or self.observation not in ('extracted', 'observed', 'interpreted'):
            raise MaterialError('invalid_block')
        if not self.block_id or self.ordinal < 0 or self.quality not in ('unassessed', 'verified', 'uncertain', 'unreadable'):
            raise MaterialError('invalid_block')


@dataclass(frozen=True)
class ExtractionManifest:
    total_units: int
    processed_units: int
    coverage: str
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.total_units) is not int or type(self.processed_units) is not int or not 0 <= self.processed_units <= self.total_units or self.coverage not in ('complete', 'partial', 'unknown', 'failed'):
            raise MaterialError('invalid_manifest')
        if self.coverage == 'complete' and self.total_units != self.processed_units:
            raise MaterialError('false_complete_coverage')


@dataclass(frozen=True)
class ExtractionBundle:
    asset_id: str
    asset_version: int
    extractor: str
    blocks: tuple[ContentBlock, ...]
    manifest: ExtractionManifest
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self):
        if self.asset_version < 1 or not self.extractor or self.contract_version != CONTRACT_VERSION:
            raise MaterialError('invalid_bundle')
        ids = {b.block_id for b in self.blocks}
        if len(ids) != len(self.blocks) or len(ids) > 4096 or sum(len(b.text) for b in self.blocks) > 2_000_000:
            raise MaterialError('extraction_budget')
        if any(b.parent_id and b.parent_id not in ids for b in self.blocks):
            raise MaterialError('missing_parent')
        parents = {b.block_id: b.parent_id for b in self.blocks}
        for bid in ids:
            visited = set()
            while bid is not None:
                if bid in visited:
                    raise MaterialError('cyclic_blocks')
                visited.add(bid)
                bid = parents[bid]

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value['blocks'] = tuple(ContentBlock(**{**b, 'locator': Locator.from_dict(b['locator']), 'limitations': tuple(b.get('limitations', ()))}) for b in value['blocks'])
        value['manifest'] = ExtractionManifest(**{**value['manifest'], 'limitations': tuple(value['manifest'].get('limitations', ()))})
        return cls(**value)


@dataclass(frozen=True)
class EvidenceRef:
    asset_id: str
    asset_version: int
    extraction_id: str
    block_id: str
    locator: Locator

    def __post_init__(self):
        if not self.asset_id or not self.extraction_id or not self.block_id or type(self.asset_version) is not int or self.asset_version < 1:
            raise MaterialError('invalid_evidence_reference')


def block_id(asset_id, version, kind, locator, ordinal=0):
    return sha256(canonical([asset_id, version, kind, asdict(locator), ordinal]).encode()).hexdigest()[:24]


def context_identity(persona_id, chat_id, topic_id, mode, scene_id):
    return sha256(canonical([persona_id, chat_id, topic_id, mode, scene_id]).encode()).hexdigest()
