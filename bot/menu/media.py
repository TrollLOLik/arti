"""Media choices travel through the durable menu's owner-checked callbacks."""
from telegram import InlineKeyboardMarkup
from bot.buttons import inline_button
from materials.types import MaterialError


def choice_keyboard(kind, step, options, columns=2):
    buttons = [inline_button(label, callback_data=f'media_choice:{kind}:{step}:{index}')
               for index, label in enumerate(options)]
    return InlineKeyboardMarkup([buttons[i:i + columns] for i in range(0, len(buttons), columns)])


def selected_choice(update, kind, step, options):
    query = getattr(update, 'callback_query', None)
    if query is None:
        return None
    parts = (query.data or '').split(':')
    if len(parts) != 4 or parts[:3] != ['media_choice', kind, step]:
        raise MaterialError('menu_stale_button')
    try:
        index = int(parts[3])
    except ValueError:
        raise MaterialError('menu_stale_button') from None
    if not 0 <= index < len(options):
        raise MaterialError('menu_stale_button')
    return options[index]
