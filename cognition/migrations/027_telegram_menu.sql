CREATE TABLE arti_menu_sessions (
 id TEXT PRIMARY KEY,chat_id BIGINT NOT NULL,topic_id BIGINT NOT NULL,user_id BIGINT NOT NULL,
 scope_key TEXT NOT NULL,message_id BIGINT,revision INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'new' CHECK(status IN('new','sending','active','unknown','gone','closed')),
 screen TEXT NOT NULL DEFAULT 'home',state JSONB NOT NULL DEFAULT '{}',actions JSONB NOT NULL DEFAULT '{}',
 content_hash TEXT,expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW()+INTERVAL '1 day',
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(chat_id,topic_id,user_id)
);
CREATE TABLE arti_menu_requests (
 session_id TEXT NOT NULL REFERENCES arti_menu_sessions(id) ON DELETE CASCADE,
 request_id TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(session_id,request_id)
);
CREATE INDEX arti_menu_expiry ON arti_menu_sessions(expires_at);
