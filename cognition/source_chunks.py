"""Bounded source-backed observation windows; no claim or author inference."""

CHUNK_CHARS = 640
CHUNK_OVERLAP = 100
MAX_CHUNKS = 32


def source_chunks(text):
    text = str(text or '')
    if not text:
        return []
    starts = list(range(0,max(1,len(text)-CHUNK_OVERLAP),CHUNK_CHARS-CHUNK_OVERLAP))
    if len(starts)>MAX_CHUNKS:
        # Bounded CPU work even for the maximum ledger input. Include both ends
        # and evenly distributed interior windows; never silently prefix-only.
        starts = [starts[round(i*(len(starts)-1)/(MAX_CHUNKS-1))] for i in range(MAX_CHUNKS)]
    return [(start,min(len(text),start+CHUNK_CHARS),text[start:start+CHUNK_CHARS]) for start in starts]


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
