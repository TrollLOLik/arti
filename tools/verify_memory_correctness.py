"""Provider-free memory regressions with an explicitly supplied local encoder.

Run only through a disposable test database. Reports contain counts/identities,
never source messages, generated answers, credentials or working history.
"""
import argparse
import json
import os
import time
import unittest
from pathlib import Path

from cognition.semantic import LocalEncoder,REVISION
from tests.cognition.test_memory_correctness import MemoryCorrectnessTests


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-dir',required=True)
    parser.add_argument('--report',default='docs/evaluation/memory_correctness_encoder.json')
    args=parser.parse_args()
    if os.getenv('ARTI_TEST_DB')!='1':
        raise SystemExit('ARTI_TEST_DB=1 and disposable PostgreSQL are required')
    MemoryCorrectnessTests.encoder_factory=staticmethod(lambda:LocalEncoder(args.model_dir))
    started=time.perf_counter()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(MemoryCorrectnessTests)
    result=unittest.TextTestRunner(verbosity=1,buffer=True).run(suite)
    report=dict(tests=result.testsRun,passed=result.testsRun-len(result.errors)-len(result.failures)-len(result.skipped),
        failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
        duration_seconds=round(time.perf_counter()-started,3),encoder_revision=REVISION,
        encoder='real pinned multilingual MiniLM, local CPU',interpretations='recorded synthetic fixtures',
        answer_generation_calls=0,provider_calls=0,production_database_mutated=False,
        scope='retrieval, correction lineage, temporal prompt grounding, fidelity, source chunks and erasure',
        limitations=['Not an evaluation of live LLM interpretation or generated answer accuracy.'])
    Path(args.report).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))
    return int(not result.wasSuccessful() or bool(result.skipped))


if __name__=='__main__':
    raise SystemExit(main())
