"""Real decoder/OCR on owned synthetic video; not a real video understanding score."""
import asyncio,json,time
from pathlib import Path
from materials.extractors.video import VideoExtractor
from materials.extractors.images import ImageExtractor
from tests.materials.video_fixtures import video

async def main():
    started=time.perf_counter(); cases=[]
    data=video(with_audio=False)
    extractor=VideoExtractor(max_frames=6,image=ImageExtractor(max_lines=12))
    bundle=await extractor.extract_async('synthetic',1,data,'video/mp4')
    timestamps=[b.locator.start_ms for b in bundle.blocks if b.metadata.get('role')=='video_frame']
    cases.append(dict(case='native_pts_scenes',passed=timestamps==[0,1000,1500,2000,2900],timestamps_ms=timestamps))
    cases.append(dict(case='screen_text',passed=all(any('SLIDE '+str(i) in b.text for b in bundle.blocks) for i in (1,2,3))))
    dense=await VideoExtractor(max_frames=12,image=ImageExtractor(ocr_enabled=False)).extract_authorized('synthetic',1,data,'video/mp4',validate=None,start_ms=1350,end_ms=1500,dense=True)
    cases.append(dict(case='transient_refinement',passed=1400 in [b.locator.start_ms for b in dense.blocks if b.metadata.get('role')=='video_frame']))
    cases.append(dict(case='coverage_and_components',passed=bundle.manifest.coverage=='partial' and 'audio_not_available' in bundle.manifest.limitations))
    report=dict(contract='video-eval-1',cases=cases,passed=sum(c['passed'] for c in cases),total=len(cases),provider_calls=0,telegram_calls=0,elapsed_seconds=time.perf_counter()-started,
        limitations=['Owned synthetic slides and transient. No independent human video benchmark.','Scene boundaries are heuristics; sparse sampling does not prove absence.','Hosted pages without an authorized bounded stream adapter are unavailable; direct media is supported.'])
    Path('docs/evaluation/materials_video.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('passed','total','elapsed_seconds')}))
    if report['passed']!=report['total']: raise SystemExit(1)
if __name__=='__main__': asyncio.run(main())
