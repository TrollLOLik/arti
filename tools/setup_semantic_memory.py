"""Download and verify pinned local retrieval weights; no conversation data is used."""
import hashlib
import json
from cognition.semantic import model_directory, REPOSITORY, REVISION, FILES


def main():
    from huggingface_hub import snapshot_download
    directory = model_directory()
    snapshot_download(REPOSITORY, revision=REVISION, allow_patterns=list(FILES), local_dir=str(directory))
    manifest = dict(repository=REPOSITORY, revision=REVISION,
        sha256={name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in FILES})
    (directory/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(dict(directory=str(directory),revision=REVISION,files=len(FILES))))


if __name__ == '__main__':
    main()
