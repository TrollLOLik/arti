"""Frozen offline validation for the visual materials phase; no live providers."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from tools.verify_native_agent_scenarios import offline_guard

ROOT=Path(__file__).resolve().parents[1]
REPORTS={'docs/evaluation/visual_design_focused.json','docs/evaluation/visual_design_validation.json',
         'docs/evaluation/visual_design_samples.json',
         'docs/evaluation/native_agent_scenarios.json','docs/evaluation/native_agent_validation.json'}
FOCUSED=(
    'tests.materials.test_visual_rendering', 'tests.materials.test_document_visuals',
    'tests.materials.test_visual_delivery', 'tests.materials.test_material_cards',
    'tests.materials.test_artifacts', 'tests.materials.test_visual_quality.ContentLayoutTests',
    'tests.materials.test_agent_acceptance', 'tests.materials.test_native_agent_safety',
    'tests.materials.test_native_agent_scenarios',
)

def source_fingerprint():
    names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT).decode().split('\0')
    names=sorted({name for name in names if name and name not in REPORTS and
        (Path(name).suffix in ('.py','.sql','.json','.sh','.yaml','.yml','.toml','.ini','.ttf') or
         name in ('.env.example','requirements.txt','requirements_db.txt','arti_card.md'))})
    entries={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() if (ROOT/name).is_file() else 'deleted' for name in names}
    return hashlib.sha256(json.dumps(entries,sort_keys=True,separators=(',',':')).encode()).hexdigest(),entries

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--suite',choices=('focused','all'),required=True)
    args=parser.parse_args(); os.chdir(ROOT); offline_guard()
    fingerprint,sources=source_fingerprint(); started=time.perf_counter()
    suite=unittest.defaultTestLoader.loadTestsFromNames(FOCUSED) if args.suite=='focused' else unittest.defaultTestLoader.discover('tests',top_level_dir='.')
    result=unittest.TextTestRunner(verbosity=1,buffer=True).run(suite)
    after,_=source_fingerprint(); unchanged=after==fingerprint
    passed=result.wasSuccessful() and not result.skipped and unchanged
    report=dict(schema='arti-visual-design-validation-1',suite=args.suite,status='passed' if passed else 'failed',
        baseline_commit=subprocess.check_output(['git','merge-base','HEAD','origin/master'],cwd=ROOT).decode().strip(),
        validated_at_utc=datetime.now(timezone.utc).isoformat(),command=f'python -m tools.verify_visual_design --suite {args.suite}',
        tests=result.testsRun,passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        failures=[t.id() for t,_ in result.failures],errors=[t.id() for t,_ in result.errors],skipped=[t.id() for t,_ in result.skipped],
        duration_seconds=round(time.perf_counter()-started,3),code_fingerprint_sha256=fingerprint,
        source_fingerprint_verified_after_suite=unchanged,source_files_sha256=sources,
        validation_environment=dict(database='disposable arti_cognition_test_<uuid> on isolated PostgreSQL',
            network='process audit guard denies non-loopback connections',dotenv_loaded=False,
            providers='offline mocks only',telegram='fake transport only',real_conversations_read=0,production_database_mutated=False),
        limitations=['Synthetic data and fake transports do not establish live model, Telegram, or production quality.',
            'Pixel QA and source-bound demo generation are recorded separately.'])
    path=ROOT/'docs/evaluation'/('visual_design_focused.json' if args.suite=='focused' else 'visual_design_validation.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='source_files_sha256'},ensure_ascii=False)); return 0 if passed else 1

if __name__=='__main__': raise SystemExit(main())
