"""Complete source indexing and bounded, source-faithful retrieval windows.

All offsets are Python/PostgreSQL character offsets, never UTF-8 byte offsets.
Windows are observations of the stored source, not inferred claims or authors.
"""

from operator import index
from uuid import uuid4


CHUNK_CHARS = 640
CHUNK_OVERLAP = 100
CHUNK_STRIDE = CHUNK_CHARS - CHUNK_OVERLAP
# Keep in sync with arti_memory_search_text in migration 034. Unlike casefold,
# this normalization is one character to one character, so every source offset
# remains valid even in databases initialized with a C locale.
_CYRILLIC_CASE = str.maketrans(
    'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ',
    'абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
)


def source_chunk_count(text):
    """Count every deterministic source window without materializing any of them."""
    length = len(str(text or ''))
    return 0 if not length else (max(1, length - CHUNK_OVERLAP) + CHUNK_STRIDE - 1) // CHUNK_STRIDE


def source_chunks(text, *, start_index=0, limit=None):
    """Return full-coverage windows, or one bounded resumable batch of windows.

    ``start_index`` is a window index, not a character offset. A page is exactly
    the corresponding slice of an unpaged call. No source-length cap or sampling
    is applied, and asking for a small page does not enumerate earlier windows.
    """
    text = str(text or '')
    start_index = _nonnegative(start_index, 'start_index')
    if limit is not None:
        limit = _nonnegative(limit, 'limit')
    count = source_chunk_count(text)
    stop_index = count if limit is None else min(count, start_index + limit)
    return [
        (start, min(len(text), start + CHUNK_CHARS), text[start:start + CHUNK_CHARS])
        for chunk_index in range(start_index, stop_index)
        for start in (chunk_index * CHUNK_STRIDE,)
    ]


def _nonnegative(value, name):
    value = index(value)
    if value < 0:
        raise ValueError(f'{name} must be nonnegative')
    return value


def windows_from_spans(text, spans, *, width=CHUNK_CHARS, limit=3):
    """Turn ordered matching character spans into distinct contextual windows.

    Input order expresses relevance. A match already fully shown by a previous
    window does not consume another result. Each accepted window has at most
    ``width`` characters and is sliced directly from the original source.
    """
    text = str(text or '')
    width = index(width)
    if width <= 0:
        raise ValueError('width must be positive')
    limit = _nonnegative(limit, 'limit')
    if not text or not limit:
        return []
    result = []
    for start, end in spans:
        start, end = index(start), index(end)
        if start < 0 or end <= start or end > len(text):
            continue
        if any(left <= start and end <= right for left, right, _ in result):
            continue
        # Prefer a little preceding context; retain all of a matched token when
        # it fits, including tokens close to either source boundary.
        left = min(max(0, start - width // 4), max(0, len(text) - width))
        if end - start <= width and end > left + width:
            left = end - width
        right = min(len(text), left + width)
        if (left, right) not in ((a, b) for a, b, _ in result):
            result.append((left, right, text[left:right]))
        if len(result) >= limit:
            break
    return result


def deduplicate_windows(text, windows, *, limit=3):
    """Merge ranked lexical/semantic windows without repeated near-identical text.

    First occurrence wins; a later window is redundant when at least 75% of its
    shorter extent overlaps an accepted one. Content alone is never the key:
    equal text at distant source positions remains separate evidence. Incoming
    excerpt strings are ignored, so the returned offsets and text always agree.
    """
    text = str(text or '')
    limit = _nonnegative(limit, 'limit')
    if not text or not limit:
        return []
    result = []
    for window in windows:
        start, end = index(window[0]), index(window[1])
        if start < 0 or end <= start or end > len(text):
            continue
        if any(4 * max(0, min(end, right) - max(start, left)) >=
               3 * min(end - start, right - left) for left, right, _ in result):
            continue
        result.append((start, end, text[start:end]))
        if len(result) >= limit:
            break
    return result


def _headline_markers(text):
    # Randomness affects only transport delimiters, never resulting windows.
    # Check collisions even though UUID collisions are already extremely rare.
    while True:
        nonce = uuid4().hex
        start, stop = f'ARTI_{nonce}_START', f'ARTI_{nonce}_STOP'
        if start not in text and stop not in text:
            return start, stop


def _headline_spans(text, headline, start_marker, stop_marker):
    """Decode PostgreSQL highlights only if every original character survives.

    HighlightAll preserves markup and spacing. Fail closed if the server ever
    changes that behavior or returns malformed/nested markers; never guess the
    positions by finding a repeated substring in the source.
    """
    if not isinstance(headline, str):
        return []
    spans = []
    source_cursor = headline_cursor = 0
    while True:
        opening = headline.find(start_marker, headline_cursor)
        if opening < 0:
            tail = headline[headline_cursor:]
            if stop_marker in tail or text[source_cursor:] != tail:
                return []
            return spans
        ordinary = headline[headline_cursor:opening]
        if stop_marker in ordinary or not text.startswith(ordinary, source_cursor):
            return []
        source_cursor += len(ordinary)
        content_start = opening + len(start_marker)
        closing = headline.find(stop_marker, content_start)
        if closing < 0:
            return []
        matched = headline[content_start:closing]
        if not matched or start_marker in matched or not text.startswith(matched, source_cursor):
            return []
        spans.append((source_cursor, source_cursor + len(matched)))
        source_cursor += len(matched)
        headline_cursor = closing + len(stop_marker)


async def lexical_windows(conn, text, query, *, width=CHUNK_CHARS, limit=3):
    """Find inflection-aware source excerpts with the same Russian FTS as recall.

    ``query`` uses PostgreSQL ``websearch_to_tsquery`` syntax; callers may pass
    the same OR-joined terms used to select candidate sources. SQL verifies the
    whole query first, since ts_headline alone can highlight partial/negative
    queries even when the document does not match. HighlightAll plus checked
    delimiters supplies exact offsets without substring/stem approximations.
    Fixed-length Cyrillic case normalization also handles C-locale PostgreSQL;
    displayed excerpts are still sliced from the untouched original source.

    The query processes the complete source, while returned excerpts are bounded
    by ``limit`` and ``width``. Database errors/timeouts belong to the caller's
    optional retrieval budget and are not disguised as successful empty results.
    """
    text, query = str(text or ''), str(query or '')
    width = index(width)
    if width <= 0:
        raise ValueError('width must be positive')
    limit = _nonnegative(limit, 'limit')
    if not text or not query.strip() or not limit:
        return []
    normalized = text.translate(_CYRILLIC_CASE)
    start, stop = _headline_markers(normalized)
    headline = await conn.fetchval("""
        WITH search AS (
            SELECT websearch_to_tsquery('russian', arti_memory_search_text($2::text)) AS terms,
                   arti_memory_search_text($1::text) AS source
        )
        SELECT CASE WHEN numnode(terms) > 0 AND to_tsvector('russian', source) @@ terms
            THEN ts_headline('russian', source, terms, $3::text) END
        FROM search
        """, text, query, f'HighlightAll=true, StartSel={start}, StopSel={stop}')
    return windows_from_spans(text, _headline_spans(normalized, headline, start, stop), width=width, limit=limit)


def chunk_trace(trace, text, start, end):
    """A stored utterance excerpt is an observation, not an extracted fact.

    Use the original observation clock. Source indexing/re-reading does not
    rehearse the trace or reset its quality decay.
    """
    excerpt = text[start:end]
    return {**trace,'gist':excerpt,'record_start':start,'record_end':end,'source_chunk':True,
        'source_prefix':text[:240],'evidence_status':'observed_message_excerpt_not_personal_fact',
        '_source_prefix_details':[d for d in trace.get('details',()) if d.get('kind') not in ('gist','wording') and d.get('start',0)<240],
        'details':[dict(text=excerpt,start=start,end=end,kind='gist',centrality=.5,
            confidence=.5,fidelity=1.,strength=.45,stability_days=30.,fidelity_stability_days=30.,
            vividness=.3,last_recalled=None,recall_count=0)]+[d for d in trace.get('details',())
                if d.get('kind') not in ('gist','wording') and d.get('start',0)<end and d.get('end',len(text))>start]}
