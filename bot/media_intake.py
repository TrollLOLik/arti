"""Live-only capability to remove specific app-generated temporary input files.

Never encoded in a durable descriptor. Merely containing 'temp' in a path is
not authority to delete. Permanent samples and external paths are preserved.
"""
from dataclasses import dataclass
from pathlib import Path
import os
import re
import stat

_ROOT=Path(__file__).resolve().parent.parent/'temp'
_NAMES=re.compile(r'^(?:dub_input_-?\d+_\d+_[0-9a-f]{6}\.[a-zA-Z0-9]{1,8}|saved_voice_\d+_\d+_[0-9a-f]{6}\.wav|vclone_ref_[0-9a-f]{8,32}(?:[_ ().a-zA-Z0-9-]*)\.wav)$')


def _identity(path,root):
    path=Path(os.path.abspath(path)); root=Path(os.path.abspath(root))
    if path.parent!=root or not _NAMES.fullmatch(path.name): return None
    for item in (*reversed(path.parents),path):
        try: info=item.lstat()
        except OSError:return None
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:return None
    if not stat.S_ISREG(info.st_mode):return None
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)


@dataclass(frozen=True)
class OwnedIntake:
    root: Path
    files: tuple

    def cleanup(self):
        for path,identity in self.files:
            if _identity(path,self.root)==identity:
                try:path.unlink()
                except OSError:pass


def owned_intake(*paths,root=None):
    root=Path(root) if root is not None else _ROOT
    result=[]
    for raw in paths:
        if raw is None:continue
        path=Path(os.path.abspath(raw)); identity=_identity(path,root)
        if identity is not None:result.append((path,identity))
    return OwnedIntake(root,tuple(result))
