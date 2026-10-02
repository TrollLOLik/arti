"""Reproducible offline suite + real disposable PostgreSQL, no working DB writes."""
import argparse
import json
import os
from pathlib import Path
import time
import unittest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    os.environ['ARTI_TEST_DB'] = '1'
    suite = unittest.defaultTestLoader.discover('tests' if args.all else 'tests/materials', top_level_dir='.')
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=1, buffer=True).run(suite)
    report = dict(suite='all' if args.all else 'materials', tests=result.testsRun,
        passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        failures=[t.id() for t,_ in result.failures],errors=[t.id() for t,_ in result.errors],
        skipped=[t.id() for t,_ in result.skipped],duration_seconds=round(time.perf_counter()-started,2),
        provider_calls=0,working_database_mutated=False,database='disposable arti_cognition_test_<uuid>')
    path=Path('docs/evaluation/materials_'+('full' if args.all else 'core')+'_tests.json')
    path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__=='__main__':
    raise SystemExit(main())
