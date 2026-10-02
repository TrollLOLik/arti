"""Opaque, bounded durable media staging in app-owned namespaces.

Descriptors contain no restored absolute path. Sources are copied, never moved
or removed. Callers retain request authorization/fencing responsibility. The
quota covers staging and measured work files, not arbitrary decoder disk writes.
"""
from contextlib import contextmanager
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
import uuid

_NAMESPACE=re.compile(r'^[0-9a-f]{32}$')
_DIGEST=re.compile(r'^[0-9a-f]{64}$')
_SUFFIXES=frozenset(('.bin','.wav','.mp3','.ogg','.opus','.m4a','.flac','.mp4','.webm','.mov','.mkv','.avi','.aac','.srt','.ass','.vtt','.png','.jpg','.jpeg','.webp'))
_LOCAL_LOCK=threading.RLock()
_MARKER='.owner.json'
_USE_LOCK='.use.lock'
_HELD=contextvars.ContextVar('arti_media_spool_holds',default={})
_CHUNK=1024*1024


class SpoolError(ValueError): pass


def default_root(*,platform=None,environ=None,home=None):
    environ=os.environ if environ is None else environ
    if environ.get('ARTI_MEDIA_SPOOL_DIR'):
        root=Path(environ['ARTI_MEDIA_SPOOL_DIR'])
        if not root.is_absolute(): raise SpoolError('spool_root_must_be_absolute')
        return root
    home=Path.home() if home is None else Path(home)
    if (platform or sys.platform)=='win32':
        base=Path(environ.get('LOCALAPPDATA') or home/'AppData'/'Local')
        return base/'Arti'/'media'
    base=Path(environ.get('XDG_DATA_HOME') or home/'.local'/'share')
    if not base.is_absolute(): base=home/'.local'/'share'
    return base/'arti'/'media'


def _unsafe(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400))


def _check_path(path,*,directory=False,missing_ok=False):
    path=Path(os.path.abspath(path))
    for part in (*reversed(path.parents),path):
        try: info=part.lstat()
        except FileNotFoundError:
            if missing_ok: return None
            raise SpoolError('spool_path_unavailable') from None
        except OSError: raise SpoolError('spool_path_unavailable') from None
        if _unsafe(info): raise SpoolError('spool_link_denied')
        if part!=path and not stat.S_ISDIR(info.st_mode): raise SpoolError('spool_parent_invalid')
    if directory and not stat.S_ISDIR(info.st_mode): raise SpoolError('spool_directory_required')
    return info


def _mkdir(path):
    path=Path(os.path.abspath(path))
    for part in (*reversed(path.parents),path):
        try: part.mkdir(mode=0o700)
        except FileExistsError: pass
        _check_path(part,directory=True)


@contextmanager
def _open_regular(path,flags=os.O_RDONLY,mode=0o600):
    path=Path(path); _check_path(path.parent,directory=True)
    try: existing=path.lstat()
    except FileNotFoundError: existing=None
    if existing is not None and (_unsafe(existing) or not stat.S_ISREG(existing.st_mode)):
        raise SpoolError('spool_special_file_denied')
    try:
        fd=os.open(path,flags|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0),mode)
    except OSError: raise SpoolError('spool_open_failed') from None
    try:
        actual=os.fstat(fd); named=_check_path(path)
        if _unsafe(actual) or not stat.S_ISREG(actual.st_mode) or (actual.st_dev,actual.st_ino)!=(named.st_dev,named.st_ino):
            raise SpoolError('spool_file_changed')
        with os.fdopen(fd,'rb' if flags==os.O_RDONLY else 'r+b') as stream:
            fd=None
            yield stream
    finally:
        if fd is not None: os.close(fd)


class MediaSpool:
    def __init__(self,root=None,*,max_file_bytes=512*1024**2,max_total_bytes=2*1024**3,max_entries=8192):
        self.root=Path(os.path.abspath(root if root is not None else default_root()))
        if not 1<=max_file_bytes<=2*1024**3 or not max_file_bytes<=max_total_bytes<=32*1024**3 or not 16<=max_entries<=100000:
            raise SpoolError('spool_invalid_limits')
        self.max_file_bytes=max_file_bytes; self.max_total_bytes=max_total_bytes; self.max_entries=max_entries

    @contextmanager
    def _lock(self):
        with _LOCAL_LOCK:
            _mkdir(self.root)
            with _open_regular(self.root/'.lock',os.O_RDWR|os.O_CREAT) as stream:
                if os.fstat(stream.fileno()).st_size==0:
                    stream.write(b'0'); stream.flush()
                end=time.monotonic()+10
                while True:
                    try:
                        if os.name=='nt':
                            import msvcrt
                            stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic()>=end: raise SpoolError('spool_busy') from None
                        time.sleep(.05)
                try: yield
                finally:
                    if os.name=='nt':
                        import msvcrt
                        stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

    def _namespace(self,namespace):
        if not isinstance(namespace,str) or not _NAMESPACE.fullmatch(namespace): raise SpoolError('spool_namespace_invalid')
        path=self.root/namespace; _check_path(path,directory=True)
        with _open_regular(path/_MARKER) as stream:
            try: marker=json.loads(stream.read(257))
            except (ValueError,UnicodeDecodeError): raise SpoolError('spool_namespace_unowned') from None
        if marker!={'arti_media_spool':1,'namespace':namespace}: raise SpoolError('spool_namespace_unowned')
        return path

    def _scan(self,path,*,missing_ok=False):
        # Accounting may race a decoder removing intermediates. Only ENOENT
        # is tolerated; unsafe replacements and permission errors still fail.
        entries=[]; pending=[Path(path)]; visited=0
        while pending:
            directory=pending.pop()
            if _check_path(directory,directory=True,missing_ok=missing_ok) is None: continue
            try:
                with os.scandir(directory) as listing:
                    for entry in listing:
                        visited+=1
                        if visited>self.max_entries: raise SpoolError('spool_scan_budget')
                        p=Path(entry.path)
                        try: info=entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            if missing_ok: continue
                            raise
                        if _unsafe(info): raise SpoolError('spool_link_denied')
                        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)): raise SpoolError('spool_special_file_denied')
                        entries.append((p,info))
                        if stat.S_ISDIR(info.st_mode): pending.append(p)
            except FileNotFoundError:
                if not missing_ok: raise
        return entries

    def create_namespace(self):
        with self._lock():
            self._scan(self.root,missing_ok=True)
            namespace=uuid.uuid4().hex; path=self.root/namespace
            path.mkdir(mode=0o700)
            marker=json.dumps({'arti_media_spool':1,'namespace':namespace},sort_keys=True).encode()
            with _open_regular(path/_MARKER,os.O_RDWR|os.O_CREAT|os.O_EXCL) as stream:
                stream.write(marker); stream.flush(); os.fsync(stream.fileno())
            with _open_regular(path/_USE_LOCK,os.O_RDWR|os.O_CREAT|os.O_EXCL) as stream:
                stream.write(b'0'); stream.flush(); os.fsync(stream.fileno())
            return namespace

    def workdir(self,namespace):
        # Existing namespace only. Never recreate a namespace removed by cleanup.
        return self._namespace(namespace)

    @contextmanager
    def _lease(self,namespace):
        path=self._namespace(namespace)
        with _open_regular(path/_USE_LOCK,os.O_RDWR) as stream:
            try:
                if os.name=='nt':
                    import msvcrt
                    stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError: raise SpoolError('spool_namespace_busy') from None
            try:
                self._namespace(namespace)
                yield path
            finally:
                if os.name=='nt':
                    import msvcrt
                    stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

    @contextmanager
    def hold(self,namespace):
        """Execution/read lease. Keep alive until every child writer has stopped.

        Acquisition is nonblocking. Nested operations in this execution context
        (including asyncio.to_thread) reuse the lease; cleanup never reuses it.
        """
        key=(str(self.root),namespace); prior=_HELD.get().get(key)
        if prior is not None and prior['active']:
            yield self._namespace(namespace)
            return
        with self._lease(namespace) as path:
            state={'active':True}; token=_HELD.set({**_HELD.get(),key:state})
            try: yield path
            finally:
                state['active']=False; _HELD.reset(token)

    def size_bytes(self,namespace):
        """Bounded sampled accounting, not a hard quota on external processes."""
        with self.hold(namespace):
            return sum(info.st_size for _,info in self._scan(self._namespace(namespace),missing_ok=True) if stat.S_ISREG(info.st_mode))

    def stage(self,source,*,namespace=None,suffix=None,max_bytes=None,validate=None):
        if namespace is None: namespace=self.create_namespace()
        source=Path(os.path.abspath(source))
        explicit_suffix=suffix is not None
        suffix=source.suffix.lower() if suffix is None else suffix
        if suffix not in _SUFFIXES:
            if explicit_suffix: raise SpoolError('spool_suffix_invalid')
            suffix='.bin'
        cap=self.max_file_bytes if max_bytes is None else max_bytes
        if type(cap) is not int or not 1<=cap<=self.max_file_bytes: raise SpoolError('spool_invalid_input_cap')
        with self._lock():
            target=self._namespace(namespace)
            if validate is not None and validate() is False: raise SpoolError('spool_request_revoked')
            with _open_regular(source) as stream:
                before=os.fstat(stream.fileno())
                if not 0<before.st_size<=cap: raise SpoolError('spool_input_too_large')
                used=sum(info.st_size for _,info in self._scan(self.root,missing_ok=True) if stat.S_ISREG(info.st_mode))
                if used+before.st_size>self.max_total_bytes: raise SpoolError('spool_disk_quota')
                leaf=uuid.uuid4().hex+suffix; temporary=target/(leaf+'.part'); final=target/leaf
                digest=hashlib.sha256(); size=0
                try:
                    with _open_regular(temporary,os.O_RDWR|os.O_CREAT|os.O_EXCL) as output:
                        while True:
                            chunk=stream.read(min(_CHUNK,cap-size+1))
                            if not chunk: break
                            size+=len(chunk)
                            if size>cap or size>before.st_size: raise SpoolError('spool_source_changed')
                            output.write(chunk); digest.update(chunk)
                        after=os.fstat(stream.fileno())
                        if size!=before.st_size or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns): raise SpoolError('spool_source_changed')
                        output.flush(); os.fsync(output.fileno())
                    self._namespace(namespace)
                    if validate is not None and validate() is False: raise SpoolError('spool_request_revoked')
                    os.replace(temporary,final)
                    # Make the directory entry durable on platforms supporting it.
                    if os.name!='nt':
                        fd=os.open(target,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
                        try: os.fsync(fd)
                        finally: os.close(fd)
                    return dict(version=1,namespace=namespace,leaf=leaf,size=size,sha256=digest.hexdigest())
                finally:
                    if temporary.exists(): temporary.unlink()

    def _descriptor(self,value):
        if not isinstance(value,dict) or set(value)!={'version','namespace','leaf','size','sha256'} or type(value['version']) is not int or value['version']!=1:
            raise SpoolError('spool_descriptor_invalid')
        leaf=value['leaf']
        if not isinstance(leaf,str) or not re.fullmatch(r'[0-9a-f]{32}\.[a-z0-9]{2,4}',leaf) or Path(leaf).suffix not in _SUFFIXES:
            raise SpoolError('spool_descriptor_invalid')
        if type(value['size']) is not int or not 0<value['size']<=self.max_file_bytes or not isinstance(value['sha256'],str) or not _DIGEST.fullmatch(value['sha256']):
            raise SpoolError('spool_descriptor_invalid')
        return self._namespace(value['namespace'])/leaf

    @contextmanager
    def open_verified(self,descriptor):
        path=self._descriptor(descriptor)
        with self.hold(descriptor['namespace']), _open_regular(path) as stream:
            before=os.fstat(stream.fileno())
            if before.st_size!=descriptor['size']: raise SpoolError('spool_integrity_failure')
            digest=hashlib.sha256(); size=0
            while True:
                chunk=stream.read(min(_CHUNK,descriptor['size']-size+1))
                if not chunk: break
                size+=len(chunk)
                if size>descriptor['size']: raise SpoolError('spool_integrity_failure')
                digest.update(chunk)
            after=os.fstat(stream.fileno())
            if size!=descriptor['size'] or digest.hexdigest()!=descriptor['sha256'] or before.st_mtime_ns!=after.st_mtime_ns: raise SpoolError('spool_integrity_failure')
            stream.seek(0)
            yield stream

    def resolve(self,descriptor):
        """Verified app-owned path; caller must hold namespace while using it."""
        with self.open_verified(descriptor):
            return self._descriptor(descriptor)

    def copy_to(self,descriptor,namespace):
        # A second app-owned staged copy, never an arbitrary restoration path.
        with self.open_verified(descriptor):
            copied=self.stage(self._descriptor(descriptor),namespace=namespace,suffix=Path(descriptor['leaf']).suffix)
        if (copied['size'],copied['sha256'])!=(descriptor['size'],descriptor['sha256']):
            with self._lock():
                path=self._descriptor(copied); _check_path(path); path.unlink()
            raise SpoolError('spool_integrity_failure')
        return copied

    def _cleanup_locked(self,namespace):
        path=self.root/namespace
        if not path.exists() and not path.is_symlink(): return True
        self._namespace(namespace)
        # Always take a fresh exclusive lease; never reuse this context's hold.
        with self._lease(namespace):
            entries=self._scan(path)
            for child,info in sorted(entries,key=lambda row:len(row[0].parts),reverse=True):
                if child.parent==path and child.name in (_MARKER,_USE_LOCK): continue
                current=_check_path(child,directory=stat.S_ISDIR(info.st_mode))
                if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino): raise SpoolError('spool_file_changed')
                if stat.S_ISDIR(info.st_mode): child.rmdir()
                else: child.unlink()
            # Removing the marker fences new acquisitions before releasing the
            # Windows handle (which cannot itself be deleted while still open).
            _check_path(path/_MARKER); (path/_MARKER).unlink()
        _check_path(path/_USE_LOCK); (path/_USE_LOCK).unlink()
        path.rmdir()
        return True

    def cleanup(self,namespace):
        if not isinstance(namespace,str) or not _NAMESPACE.fullmatch(namespace): raise SpoolError('spool_namespace_invalid')
        with self._lock(): return self._cleanup_locked(namespace)

    def collect(self,live_namespaces,*,min_age_seconds=86400,budget=16):
        if min_age_seconds<3600 or type(budget) is not int or not 1<=budget<=128: raise SpoolError('spool_collection_limits')
        live=set(live_namespaces)
        if any(not isinstance(n,str) or not _NAMESPACE.fullmatch(n) for n in live): raise SpoolError('spool_namespace_invalid')
        candidates=[]
        with self._lock():
            with os.scandir(self.root) as entries:
                for i,entry in enumerate(entries):
                    if i>=self.max_entries: raise SpoolError('spool_scan_budget')
                    if not _NAMESPACE.fullmatch(entry.name) or entry.name in live: continue
                    info=entry.stat(follow_symlinks=False)
                    if _unsafe(info): continue
                    if stat.S_ISDIR(info.st_mode) and info.st_mtime<time.time()-min_age_seconds:
                        # workdir activity may be nested, so require all entries old.
                        path=self._namespace(entry.name)
                        if all(s.st_mtime<time.time()-min_age_seconds for _,s in self._scan(path)):
                            candidates.append(entry.name)
                    if len(candidates)>=budget: break
            removed=[]
            for namespace in candidates:
                try:
                    if self._cleanup_locked(namespace): removed.append(namespace)
                except SpoolError as exc:
                    if str(exc)!='spool_namespace_busy': raise
            return removed
