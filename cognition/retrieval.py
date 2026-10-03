"""Per-call retrieval health, without global state or source payloads.

An empty list says only that this bounded query returned no records. Coverage
and availability must be inspected before interpreting that as no evidence.
"""


class RetrievalResult(list):
    def __init__(self, rows=(), *, diagnostics=None):
        super().__init__(rows)
        self.diagnostics = dict(diagnostics or {})


def retrieval_guidance(diagnostics):
    """Trusted, payload-free instruction for the response composer."""
    if not diagnostics:
        return ''
    status = diagnostics.get('status', 'unavailable')
    if status == 'complete':
        return ('Retrieval finished within its permitted scope. An empty result means '
                'no matching evidence was found by this query, not proof an event never happened.')
    if status == 'incomplete':
        reason = 'The permitted semantic index is still being filled.'
    elif status == 'timeout':
        reason = 'A bounded retrieval step ran out of time.'
    else:
        reason = 'Local semantic retrieval is unavailable; any returned lexical evidence is partial.'
    return (reason + ' Retrieval is incomplete. Use any returned evidence with its original '
            'attribution and uncertainty. If relevant, say you could not finish checking; never '
            'claim that an empty or partial result proves there is no stored evidence.')
