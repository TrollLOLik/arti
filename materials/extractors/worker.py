"""Fixed child entry point. Never imports bot configuration or credentials."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from materials.extractors.isolation import apply_resource_limits
from materials.types import MaterialError


def main():
    root = Path(sys.argv[1]).resolve()
    request = json.loads((root / 'request.json').read_text(encoding='utf-8'))
    job = None
    try:
        job = apply_resource_limits(request['limits'])
        from materials.extractors.documents import DocumentExtractor, IsolatedNativeExtractor
        data = (root / 'original').read_bytes()
        if request['operation']=='declarative':
            from agents.sandbox import run
            value=json.loads(data); result=dict(values=run(value['program'],value['inputs']))
        elif request['operation']=='video_frames':
            from materials.extractors.video_decoder import video_frames
            from materials.validation import inspect_bytes
            inspect_bytes(data,'video',request['mime'])
            result=video_frames(data,request['mime'],**request['options'])
        elif request['operation']=='audio_analysis':
            from materials.extractors.media import audio_analysis
            from materials.validation import inspect_bytes
            inspect_bytes(data,'audio',request['mime'])
            result=audio_analysis(data,request['mime'],**request['options'])
        elif request['operation'] in ('image_extract','image_region','image_preview'):
            from materials.extractors.images import ImageExtractor
            extractor=ImageExtractor(**request['options'])
            if request['operation']=='image_extract':
                result=extractor.extract(request['asset_id'],request['version'],data,request['mime']).to_dict()
            elif request['operation']=='image_preview': result=extractor.preview(data,request['mime'])
            else:
                result=extractor.region(data,request['mime'],request['locator'],request.get('reread',False),request.get('orientation_hint'))
        elif request['operation'] == 'table_extract':
            from materials.extractors.tables import TableExtractor
            result = TableExtractor(**request['options']).extract(request['asset_id'], request['version'], data, request['mime']).to_dict()
        elif request['operation'] == 'native_extract':
            extractor = IsolatedNativeExtractor(**request['options'])
            result = extractor.extract(request['asset_id'], request['version'], data, request['mime']).to_dict()
        elif request['operation'] == 'extract':
            extractor = DocumentExtractor(**request['options'])
            result = extractor.extract(request['asset_id'], request['version'], data, request['mime']).to_dict()
        elif request['operation'] == 'region':
            extractor = DocumentExtractor(**request['options'])
            result = extractor.region(data, request['mime'], request['locator'], request.get('reread', False), request.get('resource'), request.get('orientation_hint'))
        else:
            raise MaterialError('unknown_parser_operation')
    except MaterialError as exc:
        result = {'error': exc.code}
    except Exception:
        result = {'error': 'document_parse_failed'}
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(encoded) > request['limits']['output_mb'] * 1024**2:
        encoded = b'{"error":"parser_output_budget"}'
    (root / 'result.json').write_bytes(encoded)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
