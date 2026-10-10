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
        return ('Поиск по памяти завершён в допустимых пределах. Пустой результат значит лишь, что по этому '
                'запросу совпадений не нашлось, а не что события не было.')
    if status == 'incomplete':
        reason = 'Смысловой индекс памяти ещё заполняется.'
    elif status == 'timeout':
        reason = 'Один из шагов поиска по памяти не уложился во время.'
    else:
        reason = 'Смысловой поиск по памяти сейчас недоступен; найденное по словам — лишь часть.'
    return (reason + ' Поиск неполный. Используй найденное с исходным авторством и неопределённостью. '
            'Если это важно, честно скажи, что не успела всё проверить; никогда не утверждай, что пустой '
            'или частичный результат доказывает, будто ничего не сохранилось.')
