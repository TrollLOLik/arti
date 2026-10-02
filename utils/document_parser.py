"""Telegram/file adapter for the shared, isolated structured document pipeline."""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from materials.extractors.basic import render_text
from materials.extractors.documents import configured_extractor
from materials.types import MaterialError
from materials.validation import inspect_bytes

logger = logging.getLogger(__name__)
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


async def extract_document_bytes(data, file_name, declared_mime=None):
    mime = await asyncio.to_thread(inspect_bytes, data, file_name, declared_mime, MAX_DOCUMENT_BYTES)
    extractor = configured_extractor(mime)
    if hasattr(extractor, 'extract_async'):
        bundle = await extractor.extract_async('transient-document', 1, data, mime)
    else:
        bundle = await asyncio.to_thread(extractor.extract, 'transient-document', 1, data, mime)
    return render_text(bundle)


async def extract_text_from_file(file_path: Path, file_name: str) -> str:
    """Compatibility text projection. Structured bundle is the authoritative API."""
    def read():
        with Path(file_path).open('rb') as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise MaterialError('file_size_limit')
        return data
    try:
        return await extract_document_bytes(await asyncio.to_thread(read), file_name)
    except Exception as exc:
        logger.warning('Document extraction failed: %s', getattr(exc, 'code', type(exc).__name__))
        return ''


async def extract_document_text(context, doc, source_message=None) -> Optional[str]:
    from materials.runtime import enabled, capture_document
    try:
        if enabled():
            return await capture_document(context, doc, source_message)
        if doc.file_size and doc.file_size > MAX_DOCUMENT_BYTES:
            raise MaterialError('file_size_limit')
        file = await context.bot.get_file(doc.file_id)
        if file.file_size and file.file_size > MAX_DOCUMENT_BYTES:
            raise MaterialError('file_size_limit')
        data = bytes(await file.download_as_bytearray())
        result = await extract_document_bytes(data, doc.file_name or 'document', getattr(doc,'mime_type',None))
        return result if result.strip() else None
    except Exception as exc:
        logger.warning('Document extraction failed: %s', getattr(exc, 'code', type(exc).__name__))
        return None
