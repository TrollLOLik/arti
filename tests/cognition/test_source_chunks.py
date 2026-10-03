"""Exact source coverage and morphology-aware windows; no model/provider calls."""
import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cognition.source_chunks import (
    CHUNK_CHARS, CHUNK_OVERLAP, CHUNK_STRIDE, _headline_markers,
    _headline_spans, chunk_trace, deduplicate_windows, lexical_windows,
    source_chunk_count, source_chunks, windows_from_spans,
)


class SourceChunkTests(unittest.TestCase):
    def test_counts_and_boundaries(self):
        for length, expected in [(0, 0), (1, 1), (100, 1), (101, 1),
                                 (540, 1), (640, 1), (641, 2),
                                 (1180, 2), (1181, 3), (100_000, 185)]:
            with self.subTest(length=length):
                text = 'ж' * length
                self.assertEqual(source_chunk_count(text), expected)
                chunks = source_chunks(text)
                self.assertEqual(len(chunks), expected)
                for start, end, excerpt in chunks:
                    self.assertEqual(text[start:end], excerpt)
                    self.assertGreater(end, start)
                    self.assertLessEqual(end - start, CHUNK_CHARS)
                if chunks:
                    self.assertEqual(chunks[0][0], 0)
                    self.assertEqual(chunks[-1][1], length)
                    for previous, following in zip(chunks, chunks[1:]):
                        self.assertEqual(following[0], previous[0] + CHUNK_STRIDE)
                        self.assertEqual(previous[1] - following[0], CHUNK_OVERLAP)

    def test_full_source_has_no_sampling_gaps_and_batches_are_identical(self):
        text = ('😀 e\u0301 Русский 文本 <tag>\n' * 5000) + 'unique tail'
        complete = source_chunks(text)
        self.assertGreater(len(complete), 32)
        cursor = 0
        for start, end, excerpt in complete:
            self.assertLessEqual(start, cursor)
            self.assertEqual(text[start:end], excerpt)
            cursor = max(cursor, end)
        self.assertEqual(cursor, len(text))
        batches = []
        for start in range(0, source_chunk_count(text), 7):
            batches.extend(source_chunks(text, start_index=start, limit=7))
        self.assertEqual(batches, complete)
        self.assertEqual(source_chunks(text, start_index=len(complete), limit=7), [])
        self.assertEqual(source_chunks(text, start_index=10**20, limit=7), [])
        self.assertEqual(source_chunks(text, limit=0), [])
        self.assertEqual(source_chunks(None), [])

    def test_bounded_tail_batch_does_not_enumerate_prefix_offsets(self):
        text = 'a' * 1_000_000
        with patch('cognition.source_chunks.range', wraps=range, create=True) as ranges:
            chunks = source_chunks(text, start_index=1800, limit=2)
        ranges.assert_called_once_with(1800, 1802)
        self.assertEqual([c[0] for c in chunks], [1800 * 540, 1801 * 540])

    def test_invalid_batch_parameters(self):
        for kwargs in ({'start_index': -1}, {'limit': -1}):
            with self.assertRaises(ValueError):
                source_chunks('text', **kwargs)
        with self.assertRaises(TypeError):
            source_chunks('text', start_index=1.5)

    def test_chunk_trace_retains_authorship_and_original_quality_clock(self):
        text = 'A fictional narrator says: ' + 'scene ' * 200 + 'their financing succeeded.'
        prefix_detail = {'kind': 'authorship', 'text': 'fictional narrator',
                         'start': 2, 'end': 20, 'fidelity': .7}
        trace = dict(author_id=8, subject_id=None, source_id='source', modality='quoted',
                     observed_at='2025-01-01T00:00:00+00:00', last_recalled=None,
                     recall_count=0, details=[prefix_detail])
        original = copy.deepcopy(trace)
        start, end = len(text) - 100, len(text)
        result = chunk_trace(trace, text, start, end)
        for field in ('author_id', 'subject_id', 'source_id', 'modality',
                      'observed_at', 'last_recalled', 'recall_count'):
            self.assertEqual(result[field], trace[field])
        self.assertEqual(result['gist'], text[start:end])
        self.assertEqual(result['source_prefix'], text[:240])
        self.assertEqual(result['_source_prefix_details'], [prefix_detail])
        self.assertEqual(result['evidence_status'], 'observed_message_excerpt_not_personal_fact')
        self.assertEqual(result['details'][0]['recall_count'], 0)
        self.assertEqual(result['details'][0]['fidelity'], 1.)
        self.assertEqual(trace, original)


class SourceExcerptTests(unittest.TestCase):
    def test_multiple_distant_spans_and_nearby_duplicates(self):
        text = 'x' * 4000
        spans = [(100, 110), (115, 125), (1800, 1810), (3500, 3510)]
        windows = windows_from_spans(text, spans)
        self.assertEqual(len(windows), 3)
        for position, (start, end, excerpt) in zip((100, 1800, 3500), windows):
            self.assertLessEqual(start, position)
            self.assertGreater(end, position)
            self.assertLessEqual(end - start, 640)
            self.assertEqual(excerpt, text[start:end])

    def test_boundary_spans_and_long_tokens(self):
        text = '😀 e\u0301 ' * 500
        windows = windows_from_spans(text, [(0, 1), (len(text) - 1, len(text))], width=80)
        self.assertEqual(windows[0][0], 0)
        self.assertEqual(windows[-1][1], len(text))
        for start, end, excerpt in windows:
            self.assertEqual(excerpt, text[start:end])
            self.assertEqual(len(excerpt), 80)
        token_window = windows_from_spans(text, [(80, 155)], width=80)[0]
        self.assertLessEqual(token_window[0], 80)
        self.assertGreaterEqual(token_window[1], 155)
        self.assertEqual(len(windows_from_spans(text, [(80, 200)], width=80)[0][2]), 80)
        self.assertEqual(windows_from_spans('tiny', [(0, 4)]), [(0, 4, 'tiny')])

    def test_invalid_empty_and_zero_limit_spans(self):
        self.assertEqual(windows_from_spans('abc', [(-1, 2), (2, 1), (0, 4)]), [])
        self.assertEqual(windows_from_spans('', [(0, 1)]), [])
        self.assertEqual(windows_from_spans('abc', [(0, 1)], limit=0), [])
        with self.assertRaises(ValueError):
            windows_from_spans('abc', [], width=0)
        with self.assertRaises(ValueError):
            windows_from_spans('abc', [], limit=-1)

    def test_union_deduplicates_overlapping_locations_not_equal_text(self):
        text = 'x' * 4000
        windows = [(100, 740, 'untrusted excerpt'), (110, 750, 'other'),
                   (2000, 2640, 'other'), (2500, 3140, 'other')]
        result = deduplicate_windows(text, windows)
        self.assertEqual([(a, b) for a, b, _ in result], [(100, 740), (2000, 2640), (2500, 3140)])
        self.assertTrue(all(excerpt == text[a:b] for a, b, excerpt in result))
        self.assertEqual(deduplicate_windows(text, windows, limit=1), result[:1])
        self.assertEqual(deduplicate_windows(text, windows, limit=0), [])
        self.assertEqual(deduplicate_windows(text, [(-1, 3), (3, 2), (4000, 5000)]), [])

    def test_headline_exact_offsets_and_fail_closed_reconstruction(self):
        text = '<b>😀 e\u0301 financing</b> finance\n'
        first = text.index('financing')
        second = text.index('finance', first + 9)
        headline = text.replace('financing', 'STARTfinancingSTOP').replace('finance', 'STARTfinanceSTOP')
        self.assertEqual(_headline_spans(text, headline, 'START', 'STOP'),
                         [(first, first + 9), (second, second + 7)])
        for malformed in (headline.replace('<b>', ''), headline + 'extra',
                          headline.replace('STOP', '', 1), 'STARTSTARTfinancingSTOP',
                          'STOP' + text, None):
            self.assertEqual(_headline_spans(text, malformed, 'START', 'STOP'), [])
        self.assertEqual(_headline_spans(text, text, 'START', 'STOP'), [])

    def test_marker_collision_is_detected(self):
        text = 'literal ARTI_collision_START and ARTI_collision_STOP'
        with patch('cognition.source_chunks.uuid4', side_effect=[
                SimpleNamespace(hex='collision'), SimpleNamespace(hex='fresh')]):
            self.assertEqual(_headline_markers(text), ('ARTI_fresh_START', 'ARTI_fresh_STOP'))


class SourceExcerptAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_request_skips_database(self):
        conn = SimpleNamespace(fetchval=AsyncMock())
        for text, query, kwargs in [('', 'word', {}), ('text', ' ', {}),
                                    ('text', 'word', {'limit': 0})]:
            self.assertEqual(await lexical_windows(conn, text, query, **kwargs), [])
        conn.fetchval.assert_not_awaited()

    async def test_database_errors_are_not_hidden(self):
        conn = SimpleNamespace(fetchval=AsyncMock(side_effect=TimeoutError))
        with self.assertRaises(TimeoutError):
            await lexical_windows(conn, 'source', 'query')


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class SourceExcerptSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        from cognition.repositories import ensure_schema
        await ensure_schema(self.pool)

    async def asyncTearDown(self):
        await self.db.__aexit__(None, None, None)

    async def windows(self, text, query, **kwargs):
        async with self.pool.acquire() as conn:
            return await lexical_windows(conn, text, query, **kwargs)

    async def test_russian_inflection_at_tail_beyond_old_sampling_gaps(self):
        text = ('ordinary context. ' * 6000) + 'Компания занималась финансированием проекта.'
        self.assertNotIn('финансирование', text.split())
        result = await self.windows(text, 'финансирование')
        self.assertEqual(len(result), 1)
        start, end, excerpt = result[0]
        self.assertIn('финансированием', excerpt)
        self.assertEqual(excerpt, text[start:end])
        self.assertGreater(start, 90_000)
        self.assertEqual(end, len(text))
        self.assertLessEqual(len(excerpt), 640)

    async def test_russian_inflection_without_any_literal_query_substring(self):
        text = ('ordinary context. ' * 5000) + 'Речь шла о финансировании проекта.'
        self.assertFalse('финансирование' in text)
        result = await self.windows(text, 'финансирование')
        self.assertEqual(len(result), 1)
        self.assertIn('финансировании', result[0][2])
        self.assertEqual(result[0][2], text[result[0][0]:result[0][1]])

    async def test_cyrillic_case_normalization_preserves_original_names_and_offsets(self):
        text = ('😀 e\u0301 CONTEXT. ' * 600) + '<b>Алиса</b> рассказала о ФИНАНСИРОВАНИИ проекта.'
        for query in ('алиса', 'АЛИСА', 'финансирование', 'ФИНАНСИРОВАНИЕ'):
            with self.subTest(query=query):
                result = await self.windows(text, query, width=150)
                self.assertEqual(len(result), 1)
                start, end, excerpt = result[0]
                self.assertGreater(start, 7000)
                self.assertEqual(excerpt, text[start:end])
                self.assertIn('<b>Алиса</b>', excerpt)
                self.assertIn('ФИНАНСИРОВАНИИ', excerpt)
                self.assertEqual(end, len(text))

    async def test_multiple_distant_inflected_matches(self):
        text = 'финансированием проекта. ' + ('ordinary context. ' * 200)
        text += 'Книга о финансировании науки. ' + ('ordinary context. ' * 200)
        text += 'схема финансирования закончена.'
        result = await self.windows(text, 'финансирование')
        self.assertEqual(len(result), 3)
        for term, (start, end, excerpt) in zip(
                ('финансированием', 'финансировании', 'финансирования'), result):
            self.assertIn(term, excerpt)
            self.assertEqual(text[start:end], excerpt)
        self.assertEqual(await self.windows(text, 'финансирование', limit=2), result[:2])

    async def test_unicode_markup_newlines_hyphens_preserve_offsets(self):
        text = ('😀 e\u0301 &amp; <b>context</b>\r\n' * 60)
        text += '<em>финансированием</em>\nfoo-test &lt;tail&gt;'
        result = await self.windows(text, 'финансирование OR foo OR test', width=110)
        self.assertEqual(len(result), 1)
        start, end, excerpt = result[0]
        self.assertEqual(excerpt, text[start:end])
        self.assertIn('<em>финансированием</em>', excerpt)
        self.assertIn('foo-test', excerpt)
        self.assertEqual(end, len(text))

    async def test_no_matches_partial_and_negative_queries_never_return_prefix(self):
        for query in ('finance', 'финансирование отсутствующий', '-финансирование', 'и'):
            with self.subTest(query=query):
                self.assertEqual(await self.windows('финансированием catapult.', query), [])
        self.assertEqual(await self.windows('catapult.', 'cat'), [])
        self.assertTrue(await self.windows('финансированием', 'финансирование OR absent'))

    async def test_nearby_repeated_hits_share_one_excerpt(self):
        text = 'финансированием, финансирования; о финансировании.'
        self.assertEqual(await self.windows(text, 'финансирование'), [(0, len(text), text)])

    async def test_document_boundaries_match_chunk_count_sql(self):
        async with self.pool.acquire() as conn:
            for length in (0, 1, 100, 640, 641, 1180, 1181, 100_000):
                count = await conn.fetchval('''SELECT CASE WHEN length($1::text)=0 THEN 0
                    ELSE (greatest(1, length($1::text)-100)+539)/540 END''', 'я' * length)
                self.assertEqual(count, source_chunk_count('я' * length))
