import os
import unittest
from cognition.semantic import retrieval_signals,trace_names


class RetrievalSignalsTests(unittest.TestCase):
    def test_typed_names_do_not_infer_capitalized_participants(self):
        self.assertEqual(set(),trace_names({'gist':'Alice met Bob.'}))
        self.assertEqual({'alice'},trace_names({'details':[{'kind':'name','text':'Alice'}]}))

    def test_strong_semantic_paraphrase_needs_no_shared_words(self):
        trace={'gist':'Approved budget','topic':'finance','details':[]}
        signals=retrieval_signals('funded expenditure',trace,semantic_score=.8)
        self.assertEqual(0,signals['lexical']); self.assertTrue(signals['eligible'])

    def test_mismatching_explicit_name_is_not_substituted(self):
        trace={'gist':'Bob approved budget','details':[{'kind':'name','text':'Bob'}]}
        self.assertFalse(retrieval_signals('Alice spending',trace,requested_names={'alice'},semantic_score=.95)['eligible'])

    def test_name_alone_cannot_satisfy_wrong_event(self):
        trace={'gist':'Alice approved budget','details':[{'kind':'name','text':'Alice'}]}
        self.assertFalse(retrieval_signals('Alice violin',trace,requested_names={'alice'})['eligible'])

    def test_compound_identifier_parts_remain_searchable(self):
        self.assertTrue(retrieval_signals('PRIVATE',{'gist':'PRIVATE_ALPHA'})['eligible'])

    def test_empty_query_cannot_use_accessibility(self):
        self.assertFalse(retrieval_signals('what was it',{'gist':'some unrelated event'})['eligible'])


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable SQL required')
class RetrievalContextSQLTests(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_synthetic_context_corpus(self):
        from tests.support.database import isolated_database
        from tools.evaluate_retrieval_context import evaluate
        async with isolated_database() as pool: report=await evaluate(pool)
        self.assertEqual(report['total'],report['passed'],report)
        self.assertFalse(report['actual_embedding_quality_measured'])
