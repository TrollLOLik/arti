-- Additive Mini App foundation. No existing context identities or scope hashes change.
CREATE TABLE app_users (
    id BIGINT PRIMARY KEY CHECK (id > 0),
    first_name TEXT NOT NULL CHECK (length(first_name) <= 128),
    username TEXT NOT NULL DEFAULT '' CHECK (length(username) <= 64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE app_conversations (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('arti_private','direct','room')),
    persona_id TEXT NOT NULL DEFAULT 'arti',
    owner_id BIGINT NOT NULL REFERENCES app_users(id),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    creation_key UUID,
    message_seq BIGINT NOT NULL DEFAULT 0 CHECK (message_seq >= 0),
    event_seq BIGINT NOT NULL DEFAULT 0 CHECK (event_seq >= 0),
    access_revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, creation_key)
);
CREATE UNIQUE INDEX app_one_arti_private ON app_conversations(owner_id,persona_id) WHERE kind='arti_private';
CREATE TABLE app_memberships (
    conversation_id UUID NOT NULL REFERENCES app_conversations(id),
    user_id BIGINT NOT NULL REFERENCES app_users(id),
    role TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','left','removed')),
    visible_from_seq BIGINT NOT NULL DEFAULT 0 CHECK (visible_from_seq >= 0),
    read_seq BIGINT NOT NULL DEFAULT 0 CHECK (read_seq >= 0),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id,user_id)
);
CREATE INDEX app_user_conversations ON app_memberships(user_id,conversation_id) WHERE state='active';
CREATE TABLE app_context_bindings (
    conversation_id UUID PRIMARY KEY REFERENCES app_conversations(id),
    kind TEXT NOT NULL CHECK (kind IN ('telegram_private','native_room')),
    telegram_chat_id BIGINT,
    native_id UUID UNIQUE,
    topic_id BIGINT NOT NULL DEFAULT -1 CHECK (topic_id=-1),
    mode TEXT NOT NULL DEFAULT 'default' CHECK (mode='default'),
    scene_id TEXT NOT NULL DEFAULT '' CHECK (scene_id=''),
    CHECK ((kind='telegram_private' AND telegram_chat_id IS NOT NULL AND telegram_chat_id>0 AND native_id IS NULL)
        OR (kind='native_room' AND telegram_chat_id IS NULL AND native_id IS NOT NULL))
);
CREATE UNIQUE INDEX app_private_context_binding ON app_context_bindings(telegram_chat_id) WHERE kind='telegram_private';
CREATE TABLE app_messages (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES app_conversations(id),
    seq BIGINT NOT NULL CHECK (seq>0),
    author_id BIGINT REFERENCES app_users(id),
    author_kind TEXT NOT NULL CHECK (author_kind IN ('user','arti','system')),
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 8000 AND octet_length(text)<=32000),
    client_message_id UUID,
    payload_hash TEXT NOT NULL,
    source_key TEXT NOT NULL CHECK (length(source_key)<=200),
    reply_to_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    suppressed_at TIMESTAMPTZ,
    CHECK (author_kind!='user' OR (author_id IS NOT NULL AND client_message_id IS NOT NULL)),
    UNIQUE (conversation_id,seq),
    UNIQUE (conversation_id,id),
    UNIQUE (conversation_id,author_id,client_message_id),
    UNIQUE (conversation_id,source_key),
    FOREIGN KEY (conversation_id,reply_to_id) REFERENCES app_messages(conversation_id,id)
);
CREATE TABLE app_events (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES app_conversations(id),
    seq BIGINT NOT NULL CHECK (seq>0),
    kind TEXT NOT NULL CHECK (kind IN ('message.created','membership.changed')),
    resource_id UUID,
    access_revision BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id,seq)
);
CREATE TABLE app_outbox (
    event_id UUID PRIMARY KEY REFERENCES app_events(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts>=0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX app_outbox_pending ON app_outbox(available_at) WHERE state='pending';
CREATE TABLE app_deliveries (
    id UUID PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES app_messages(id),
    target TEXT NOT NULL CHECK (target IN ('miniapp','telegram')),
    ordinal INTEGER NOT NULL DEFAULT 0 CHECK (ordinal>=0),
    state TEXT NOT NULL CHECK (state IN ('prepared','sending','delivered','delivery_unknown','cancelled')),
    receipt_id TEXT,
    UNIQUE (message_id,target,ordinal)
);
CREATE TABLE app_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES app_users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX app_session_expiry ON app_sessions(expires_at);
