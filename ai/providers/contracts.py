"""Typed media requests with real MIME and explicit resource bounds."""
import base64
from dataclasses import dataclass
from io import BytesIO
from PIL import Image
from materials.types import MaterialError
from materials.validation import inspect_bytes


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    mime: str

    def __post_init__(self):
        if not isinstance(self.data, bytes) or self.mime not in ('image/jpeg','image/png','image/webp','image/gif'):
            raise MaterialError('invalid_image_type')
        inspect_bytes(self.data, 'image', self.mime)

    @classmethod
    def from_base64(cls, value, max_bytes=10 * 1024 * 1024):
        declared = None
        if value.startswith('data:'):
            header, value = value.split(',', 1)
            if not header.endswith(';base64'):
                raise MaterialError('invalid_image_encoding')
            declared = header[5:-7]
        if len(value) > (max_bytes + 2) // 3 * 4:
            raise MaterialError('image_byte_limit')
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise MaterialError('invalid_image_encoding') from exc
        mime = inspect_bytes(data, 'image', declared, max_bytes)
        if not mime.startswith('image/'):
            raise MaterialError('invalid_image_type')
        return cls(data, mime)

    def url(self):
        return 'data:' + self.mime + ';base64,' + base64.b64encode(self.data).decode('ascii')


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    system: str = ''
    images: tuple[ImageInput, ...] = ()
    uploaded_video: object | None = None
    web_search: bool = False
    maps: bool = False
    native_tools: bool = False
    structured_output: bool = False

    def __post_init__(self):
        if len(self.images) > 8 or sum(len(i.data) for i in self.images) > 20 * 1024 * 1024:
            raise MaterialError('media_budget')

    @property
    def inputs(self):
        return frozenset({'text'} | ({'image'} if self.images else set()) | ({'video'} if self.uploaded_video is not None else set()))

    @property
    def features(self):
        return frozenset(k for k, present in [('search', self.web_search), ('maps', self.maps), ('tools', self.native_tools), ('structured_output', self.structured_output)] if present)

    def openai_messages(self):
        if self.uploaded_video is not None:
            raise MaterialError('provider_video_adapter_unavailable')
        content = self.prompt if not self.images else [{'type': 'text', 'text': self.prompt}] + [
            {'type': 'image_url', 'image_url': {'url': i.url()}} for i in self.images]
        return [{'role': 'system', 'content': self.system}, {'role': 'user', 'content': content}]

    def gemini_parts(self):
        from google.genai import types
        parts = [types.Part.from_bytes(data=i.data, mime_type=i.mime) for i in self.images]
        if self.uploaded_video is not None:
            parts.append(self.uploaded_video)
        parts.append(types.Part.from_text(text=self.prompt))
        return parts
