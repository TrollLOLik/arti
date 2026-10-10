"""Shared Telegram button accents, including clients using older PTB releases."""
import re

from telegram import InlineKeyboardButton


_DANGER_ACTIONS = frozenset((
    'delete', 'remove', 'erase', 'forget', 'cancel', 'stop', 'reject', 'decline',
    'revoke', 'clear_context', 'voice_delete', 'rp_off',
))
_SUCCESS_ACTIONS = frozenset((
    'accept', 'approve', 'confirm', 'save', 'resume', 'complete', 'publish', 'start',
))
_DANGER_LABEL = re.compile(
    r'\b(?:удалить|стереть|убрать из памяти|отменить|отмена|остановить|выключить|'
    r'очистить контекст|сбросить всё|отклонить|отказаться|отозвать|закончить сцену|'
    r'не сохранять|не разрешать)\b', re.I,
)
_SUCCESS_LABEL = re.compile(
    r'\b(?:сохранить|принять|подтвердить|разрешить|выполнить|включить|'
    r'возобновить|вернуть из архива)\b', re.I,
)


def _semantic_style(action):
    for key in ('confirm', 'perform'):
        nested = action.get(key)
        if isinstance(nested, dict):
            return _semantic_style(nested)
    for key in ('project_control', 'task_control', 'special'):
        value = action.get(key)
        if value in _DANGER_ACTIONS:
            return 'danger'
        if value in _SUCCESS_ACTIONS:
            return 'success'
    if action.get('artifact_remove'):
        return 'danger'
    if action.get('effect_approve') or action.get('procedure_approve'):
        return 'success'
    form = action.get('form', '')
    if form == 'forget' or form.rsplit('_', 1)[-1] in _DANGER_ACTIONS:
        return 'danger'
    if form.rsplit('_', 1)[-1] in _SUCCESS_ACTIONS:
        return 'success'
    command = action.get('command', '').split()
    if command:
        operation = command[0].lstrip('/').split('@', 1)[0]
        subcommand = command[1] if len(command) > 1 else None
        if operation in _DANGER_ACTIONS or subcommand in _DANGER_ACTIONS:
            return 'danger'
        if operation in _SUCCESS_ACTIONS or subcommand in _SUCCESS_ACTIONS:
            return 'success'
    return None


def button_style(text, action=None, *, form_kind=None):
    """Use the actual action for confirmations with generic labels like 'Yes'."""
    action = action or {}
    if action.get('style') in ('primary', 'success', 'danger'):
        return action['style']
    if 'url' in action:
        return 'primary'
    semantic = _semantic_style(action)
    if action.get('submit') and form_kind:
        semantic = _semantic_style(dict(form=form_kind)) or semantic
    if semantic:
        return semantic
    callback = str(action.get('legacy') or action.get('callback_data') or '')
    if callback.startswith(('forget_', 'vdel:')) or callback in (
        'photo_act:cancel', 'doc_act:cancel', 'vurl:cancel', 'model_fclear', 'vsave:no',
    ):
        return 'danger'
    if callback.startswith('media_voice_save:') or callback == 'vsave:yes':
        return 'success'
    if _DANGER_LABEL.search(text) or text.strip() == '🗑':
        return 'danger'
    if _SUCCESS_LABEL.search(text) or text.lstrip().startswith(('✅', '✓')):
        return 'success'
    return 'primary'


def inline_button(text, *, style=None, **kwargs):
    """Pass styles through api_kwargs for both PTB 21 and newer Bot API bindings."""
    api_kwargs = dict(kwargs.pop('api_kwargs', None) or {})
    chosen = style or api_kwargs.get('style') or button_style(text, kwargs)
    if chosen not in ('primary', 'success', 'danger'):
        raise ValueError('Unsupported Telegram button style')
    api_kwargs['style'] = chosen
    return InlineKeyboardButton(text, api_kwargs=api_kwargs, **kwargs)
