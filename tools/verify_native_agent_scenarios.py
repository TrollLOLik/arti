"""Freeze and validate native scenarios offline against disposable PostgreSQL.

Run with ARTI_TEST_DB=1 and explicit loopback DB_HOST/DB_NAME=postgres.
Reports contain test identifiers and source hashes, never conversation payloads.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
NATIVE_MODULES = (
    'tests.cognition.test_native_organizer_scenarios',
    'tests.cognition.test_native_agent_safety',
    'tests.materials.test_native_agent_scenarios',
    'tests.materials.test_native_agent_safety',
)


def offline_guard():
    if os.environ.get('ARTI_TEST_DB') != '1':
        raise RuntimeError('Explicit ARTI_TEST_DB=1 is required')
    host = os.environ.get('DB_HOST', '')
    if host not in ('127.0.0.1', '::1', 'localhost') or os.environ.get('DB_NAME') != 'postgres':
        raise RuntimeError('Use an explicit disposable loopback PostgreSQL server with DB_NAME=postgres')
    import dotenv
    import dotenv.main
    dotenv.load_dotenv = dotenv.main.load_dotenv = lambda *a, **k: False
    dotenv.dotenv_values = dotenv.main.dotenv_values = lambda *a, **k: {}
    for key in ('GROQ_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY'):
        os.environ[key] = 'offline-test-placeholder'
    os.environ['TELEGRAM_TOKEN'] = '123456:offline-test-placeholder'
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['HF_HUB_OFFLINE'] = os.environ['TRANSFORMERS_OFFLINE'] = '1'

    def audit(event, args):
        if event in ('socket.connect', 'socket.sendto'):
            address = args[1]
            if isinstance(address, tuple):
                try:
                    local = ipaddress.ip_address(address[0]).is_loopback
                except ValueError:
                    local = address[0] == 'localhost'
                if not local:
                    raise RuntimeError('Offline validation denied external network access')
    sys.addaudithook(audit)


def source_fingerprint():
    names = subprocess.check_output(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT
    ).decode().split('\0')
    generated_reports = {'docs/evaluation/native_agent_scenarios.json', 'docs/evaluation/native_agent_validation.json'}
    sources = sorted({name for name in names if name and name not in generated_reports
                      and (Path(name).suffix in ('.py', '.sql', '.json', '.sh', '.yaml', '.yml', '.toml', '.ini')
                           or name in ('.env.example', 'requirements.txt', 'requirements_db.txt', 'arti_card.md'))})
    entries = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
               if (ROOT / name).is_file() else 'deleted' for name in sources}
    serialized = json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(serialized).hexdigest(), entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', choices=('native', 'all'), required=True)
    args = parser.parse_args()
    os.chdir(ROOT)
    offline_guard()
    fingerprint, sources = source_fingerprint()
    base = subprocess.check_output(['git', 'merge-base', 'HEAD', 'origin/master'], cwd=ROOT).decode().strip()
    if args.suite == 'native':
        suite = unittest.defaultTestLoader.loadTestsFromNames(NATIVE_MODULES)
    else:
        suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=1, buffer=True).run(suite)
    after, _ = source_fingerprint()
    unchanged = fingerprint == after
    passed = result.wasSuccessful() and not result.skipped and unchanged
    report = dict(
        schema='native-agent-validation-1', suite=args.suite,
        status='passed' if passed else 'failed', baseline_commit=base,
        validated_at_utc=datetime.now(timezone.utc).isoformat(),
        command=f'python -m tools.verify_native_agent_scenarios --suite {args.suite}',
        tests=result.testsRun,
        passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        failures=[t.id() for t, _ in result.failures],
        errors=[t.id() for t, _ in result.errors],
        skipped=[t.id() for t, _ in result.skipped],
        duration_seconds=round(time.perf_counter()-started, 3),
        code_fingerprint_sha256=fingerprint, source_fingerprint_verified_after_suite=unchanged,
        source_files_sha256=sources,
        validation_environment=dict(database='disposable arti_cognition_test_<uuid> on isolated PostgreSQL',
            network='process audit guard denies non-loopback connections', dotenv_loaded=False,
            providers='offline mocks; non-loopback connections blocked; mocked calls are not counted',
            telegram='fake transport; non-loopback connections blocked; mocked calls are not counted',
            real_conversations_read=0,
            production_database_mutated=False),
        limitations=[
            'Synthetic requests, deterministic mocks and disposable SQL; no live model, Telegram or deployment validation.',
            'This is fresh validation of the reconstructed source fingerprint, not evidence for the missing earlier snapshot.',
            'Grammar is deliberately bounded; arbitrary language and arbitrary commitments are outside the supported contract.',
        ],
    )
    path = ROOT / 'docs/evaluation' / ('native_agent_scenarios.json' if args.suite == 'native' else 'native_agent_validation.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'source_files_sha256'}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
