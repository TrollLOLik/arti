"""Short-lived, owner-bound drafts consumed by a real /start bot update."""
import hashlib
import json
import re
import secrets

ACTIONS = frozenset(('talk', 'image', 'video', 'music', 'voices', 'memory', 'projects', 'materials', 'tasks', 'roleplay'))


class HandoffConflict(ValueError):
    pass


async def initialize(pool):
    async with pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS arti_miniapp_handoffs (
            token TEXT PRIMARY KEY, owner_id BIGINT NOT NULL CHECK(owner_id>0),
            request_key TEXT NOT NULL, action TEXT NOT NULL, prompt TEXT,
            payload_hash TEXT NOT NULL, consumed_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW()+INTERVAL '10 minutes',
            UNIQUE(owner_id,request_key));
            CREATE INDEX IF NOT EXISTS arti_miniapp_handoff_expiry ON arti_miniapp_handoffs(expires_at)''')


async def prepare(pool, owner, action, prompt, request_key):
    digest = hashlib.sha256(json.dumps([action, prompt], ensure_ascii=False).encode()).hexdigest()
    async with pool.acquire() as conn, conn.transaction():
        # Erase expired drafts, including those never opened in the chat.
        await conn.execute('DELETE FROM arti_miniapp_handoffs WHERE expires_at<=NOW()')
        row = await conn.fetchrow('''INSERT INTO arti_miniapp_handoffs(token,owner_id,request_key,action,prompt,payload_hash)
            VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(owner_id,request_key) DO UPDATE
            SET request_key=EXCLUDED.request_key RETURNING *''', secrets.token_urlsafe(24), owner, request_key, action, prompt, digest)
        if row['payload_hash'] != digest or row['consumed_at'] is not None:
            raise HandoffConflict('handoff_conflict')
        return row['token']


async def consume(pool, owner, token):
    if type(owner) is not int or owner <= 0 or not re.fullmatch(r'[A-Za-z0-9_-]{32}', token):
        return None
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow('''SELECT action,prompt FROM arti_miniapp_handoffs
            WHERE token=$1 AND owner_id=$2 AND consumed_at IS NULL AND expires_at>NOW() FOR UPDATE''', token, owner)
        if row is None:
            return None
        await conn.execute('UPDATE arti_miniapp_handoffs SET consumed_at=NOW(),prompt=NULL WHERE token=$1', token)
        return dict(row)


async def handle_start(update, context):
    """Called after normal /start authorization and spam checks. No polling here."""
    args = getattr(context, 'args', None) or []
    if len(args) != 1 or not args[0].startswith('ma_'):
        return False
    import os
    if os.getenv('ARTI_MINIAPP_ENABLED', '0').lower() not in ('1', 'true', 'yes'):
        await update.effective_message.reply_text('Mini App сейчас не включён. Напиши мне прямо в чат или открой /menu.')
        return True
    if update.effective_chat.type != 'private' or update.effective_chat.id != update.effective_user.id:
        await update.effective_message.reply_text('Открой запрос в личном чате со мной.')
        return True
    from bot.menu import pool_for_menu, _open_panel
    pool = pool_for_menu()
    if pool is None:
        await update.effective_message.reply_text('Хранилище пока недоступно. Попробуй открыть запрос позже.')
        return True
    draft = await consume(pool, update.effective_user.id, args[0][3:])
    if draft is None:
        await update.effective_message.reply_text('Этот запрос уже открыт или ссылка устарела. Вернись в приложение и открой его заново.')
        return True
    action, prompt = draft['action'], draft['prompt'] or ''
    if action not in ACTIONS:
        return True
    if action == 'talk' and prompt:
        # Preserve the actual /start message id, date, scope and sender. The draft
        # is data, so remove command entities and invoke the text handler directly.
        from telegram import Update, Message
        from bot.handlers import handle_all_messages
        message = update.effective_message.to_dict()
        message.update(text=prompt, entities=[], reply_to_message=None)
        forwarded = Update(update.update_id, message=Message.de_json(message, context.bot))
        previous = context.args
        context.args = []
        try:
            await handle_all_messages(forwarded, context)
        finally:
            context.args = previous
    elif action in ('image', 'video', 'music'):
        import shlex
        await _open_panel(update, context, command='/' + action + (' ' + shlex.quote(prompt) if prompt else ''))
    elif action == 'tasks':
        from bot.organizer_commands import command as organizer_command
        previous = context.args
        context.args = []
        try:
            await organizer_command(update, context)
        finally:
            context.args = previous
    else:
        command = dict(voices='/voices', memory='/my_profile', projects='/project list', materials='/materials_find', roleplay='/rp').get(action)
        await _open_panel(update, context, command=command)
    return True
