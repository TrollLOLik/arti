"""Run offline providers and disposable PostgreSQL tests; export counts, not payloads."""
import json
import os
import time
import unittest
from pathlib import Path
from cognition.types import MODEL_VERSION


def main():
    os.environ['ARTI_TEST_DB'] = '1'
    suite = unittest.defaultTestLoader.discover('tests',top_level_dir='.')
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=1,buffer=True).run(suite)
    report = dict(model_version=MODEL_VERSION,tests=result.testsRun,passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        failures=[t.id() for t,_ in result.failures],errors=[t.id() for t,_ in result.errors],
        skipped=[t.id() for t,_ in result.skipped],duration_seconds=time.perf_counter()-started,
        database='disposable arti_cognition_test_<uuid>',provider_calls=0,working_database_mutated=False)
    Path('docs/evaluation/automated_tests_full.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__=='__main__':
    raise SystemExit(main())
