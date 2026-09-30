"""Decoder/timing contract evaluation; provider fixtures are not ASR accuracy."""
import asyncio,json,time
from pathlib import Path
from materials.extractors.audio import AudioExtractor
from materials.timeline import assembly_timeline,groq_timeline
from tests.materials.audio_fixtures import wav,assembly_result


async def main():
    started=time.perf_counter(); results=[]
    for noise in (False,True):
        bundle=await AudioExtractor(max_seconds=1).extract_async('fixture',1,wav(noise=noise),'audio/wav')
        results.append(dict(case='noisy_decoder' if noise else 'decoder',passed=bundle.manifest.processed_units==1000 and bundle.manifest.total_units==3000 and bundle.manifest.coverage=='partial'))
    t=assembly_timeline(assembly_result(),3000)
    results.append(dict(case='native_provider_timestamp_fixture',passed=t.overlaps()[0]['start_ms']==1100 and t.segments[0].words[1].end_ms==1600))
    results.append(dict(case='untimed_refusal',passed=not groq_timeline({'text':'arbitrary speech'},3000).segments))
    fixed=t.confirm('turn_1','1500',actor_ref='user:7',expected_id=t.id)
    results.append(dict(case='correction_alignment',passed=t.segments[0].words==fixed.segments[0].words and t.id!=fixed.id))
    report=dict(contract='audio-eval-1',cases=results,passed=sum(r['passed'] for r in results),total=len(results),elapsed_seconds=time.perf_counter()-started,provider_calls=0,telegram_calls=0,
        limitations=['Synthetic tones exercise decoder and energy analysis; no speech-recognition accuracy measurement.','Provider payload fixtures exercise native timestamps and uncertainty. Independent real ASR/diarization corpus remains A24.'])
    path=Path('docs/evaluation/materials_audio.json'); path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('passed','total','provider_calls','elapsed_seconds')}))
    if report['passed']!=report['total']: raise SystemExit(1)

if __name__=='__main__': asyncio.run(main())
