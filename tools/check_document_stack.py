"""Offline OCR/parser dependency health. No user files, API keys or bot startup."""
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
from materials.extractors.ocr import installation


def health():
    command,directory=installation()
    libraries={}
    for package in ('pdfplumber','pypdfium2','python-docx','opencv-python-headless','Pillow','psutil'):
        try: libraries[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: libraries[package]='missing'
    models={}
    for lang in ('rus','eng','osd'):
        path=Path(directory)/(lang+'.traineddata')
        models[lang]=dict(present=path.is_file(),sha256=sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    version='missing'
    if command:
        try:
            result=subprocess.run([command,'--version'],capture_output=True,timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            version=(result.stdout+result.stderr).decode(errors='replace').splitlines()[0]
        except (OSError,subprocess.TimeoutExpired): version='unavailable'
    return dict(libraries=libraries,ocr_engine=version,models=models,
        ready=all(v!='missing' for v in libraries.values()) and all(m['present'] for m in models.values()) and version not in ('missing','unavailable'))


if __name__=='__main__':
    report=health(); print(json.dumps(report,indent=2)); raise SystemExit(0 if report['ready'] else 1)
