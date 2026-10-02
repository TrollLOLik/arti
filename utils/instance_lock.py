"""OS-owned poller lock and nonce-bound graceful local control, without tokens on disk."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


class AlreadyRunning(RuntimeError):
    def __init__(self, info=None):
        self.info = info or {}
        super().__init__('arti_already_running')


class InstanceLock:
    def __init__(self, token, directory=None):
        digest = hashlib.sha256(token.encode()).hexdigest()[:32]
        self.root = Path(directory) if directory else Path(tempfile.gettempdir())/'arti-runtime'
        self.path = self.root/(digest+'.lock')
        self.control = self.root/(digest+'.stop')
        self.handle = None
        self.nonce = uuid.uuid4().hex

    def info(self):
        try:
            with self.path.open('rb') as handle:
                handle.seek(1)  # Windows locks byte zero; metadata remains readable.
                value = json.loads(handle.read(4096))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def acquire(self):
        self.root.mkdir(parents=True, exist_ok=True)
        handle = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), 'r+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(self.info()) from None
        self.handle = handle
        handle.seek(0)
        handle.truncate()
        handle.write(b' '+json.dumps(dict(pid=os.getpid(), nonce=self.nonce,
            started_at=time.time(), workspace=str(Path.cwd()))).encode())
        handle.flush()
        os.fsync(handle.fileno())
        return self

    def close(self):
        if self.handle is not None:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def status(self):
        if self.handle is not None:
            return self.info()
        try:
            self.acquire()
        except AlreadyRunning as exc:
            return exc.info or {'pid': None}
        else:
            self.close()
            return None

    def request_stop(self):
        info = self.status()
        if info is None:
            return False
        if not info.get('nonce'):
            raise RuntimeError('instance_metadata_unavailable')
        temporary = self.control.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(info['nonce'], encoding='ascii')
        os.replace(temporary, self.control)
        return True

    def stop_requested(self):
        try:
            return self.control.read_text(encoding='ascii').strip() == self.nonce
        except (OSError, UnicodeError):
            return False


class PollerLease:
    """A dedicated PostgreSQL session excludes another host using this token."""
    def __init__(self, pool, token):
        self.pool = pool
        self.key = int.from_bytes(hashlib.sha256(('telegram-poller:'+token).encode()).digest()[:8], 'big', signed=True)
        self.conn = None

    async def acquire(self):
        self.conn = await self.pool.acquire()
        try:
            if not await self.conn.fetchval('SELECT pg_try_advisory_lock($1::bigint)', self.key):
                raise AlreadyRunning()
        except BaseException:
            await self.pool.release(self.conn)
            self.conn = None
            raise
        return self

    async def healthy(self):
        return self.conn is not None and not self.conn.is_closed() and await self.conn.fetchval('SELECT 1') == 1

    async def close(self):
        if self.conn is not None:
            try:
                if not self.conn.is_closed():
                    await self.conn.execute('SELECT pg_advisory_unlock($1::bigint)', self.key)
            finally:
                await self.pool.release(self.conn)
                self.conn = None
