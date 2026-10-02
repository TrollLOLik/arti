"""Opaque local blob storage with atomic writes and checked filesystem paths."""
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from materials.types import MaterialError


class LocalBlobStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        if not re.fullmatch(r'[0-9a-f]{32}', key):
            raise MaterialError('invalid_blob_key')
        path = (self.root / key[:2] / (key + '.blob')).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise MaterialError('blob_path_escape')
        return path

    def put(self, data, key=None):
        key = key or uuid.uuid4().hex
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.stage-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return key

    def read(self, key, digest, max_bytes):
        path = self.path(key)
        if not path.is_file() or path.stat().st_size > max_bytes:
            raise MaterialError('blob_missing_or_oversize')
        with path.open('rb') as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes or hashlib.sha256(data).hexdigest() != digest:
            raise MaterialError('blob_integrity')
        return data

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def old_keys(self, min_age_seconds=86400):
        if min_age_seconds < 3600:
            raise MaterialError('unsafe_orphan_age')
        bound = time.time() - min_age_seconds
        return [p.stem for p in self.root.glob('*/*.blob') if p.stat().st_mtime < bound and re.fullmatch(r'[0-9a-f]{32}', p.stem)]
