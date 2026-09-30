"""Bounded MIME and container checks before parsing any material."""
from io import BytesIO
from pathlib import PurePath
import zipfile
from PIL import Image
from materials.types import MaterialError

IMAGE_MIMES = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp', 'GIF': 'image/gif'}


def inspect_bytes(data, filename, declared_mime=None, max_bytes=10 * 1024 * 1024):
    if not data or len(data) > max_bytes:
        raise MaterialError('file_size_limit')
    suffix = PurePath(filename).suffix.lower()
    mime = None
    if data.startswith(b'%PDF-'):
        mime = 'application/pdf'
    elif data.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF8')) or data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        try:
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > 40_000_000:
                    raise MaterialError('image_pixel_limit')
                mime = IMAGE_MIMES.get(image.format)
                image.verify()
        except MaterialError:
            raise
        except Exception as exc:
            raise MaterialError('invalid_image') from exc
    elif data.startswith(b'PK'):
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 2048 or sum(e.file_size for e in entries) > 80 * 1024 * 1024:
                    raise MaterialError('archive_expansion_limit')
                if any(e.flag_bits & 1 or e.compress_size and e.file_size / e.compress_size > 300 for e in entries):
                    raise MaterialError('unsafe_archive')
                names = set(archive.namelist())
                if 'word/document.xml' in names:
                    mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                elif 'xl/workbook.xml' in names:
                    mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        except zipfile.BadZipFile as exc:
            raise MaterialError('invalid_archive') from exc
    elif data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        mime = 'audio/wav'
    elif data.startswith(b'OggS'):
        mime = 'audio/ogg'
    elif data.startswith(b'ID3') or data[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
        mime = 'audio/mpeg'
    elif len(data) >= 12 and data[4:8] == b'ftyp':
        mime = 'video/mp4'
    elif suffix in ('.txt', '.csv', '.md', '.json'):
        try:
            data.decode('utf-8-sig')
            if b'\x00' not in data:
                mime = {'csv': 'text/csv', 'json': 'application/json'}.get(suffix[1:], 'text/plain')
        except UnicodeDecodeError:
            pass
    if mime is None:
        raise MaterialError('unsupported_material')
    if declared_mime and declared_mime not in (mime, 'application/octet-stream', 'text/plain'):
        # Telegram sometimes labels UTF8 CSV as text/plain; binary conflicts fail.
        raise MaterialError('mime_mismatch')
    if declared_mime == 'text/plain' and not mime.startswith('text/'):
        raise MaterialError('mime_mismatch')
    return mime
