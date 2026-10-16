-- H6 (P2): a person's own choice about Arti's in-character life: whether she
-- mentions it to them, and whether they have seen the explainer. An explicit
-- settings source keyed by the Telegram user: only the person's own action
-- writes it (source names that action), never a chat message, so forgetting a
-- message or clearing the dialogue leaves it as it is. Only the person's own
-- reset of the setting or the erasure of their data removes the row.
CREATE TABLE IF NOT EXISTS arti_life_settings (
    user_id BIGINT PRIMARY KEY CHECK (user_id > 0),
    mentions BOOLEAN NOT NULL DEFAULT TRUE,
    explainer_seen_at TIMESTAMPTZ,
    source TEXT NOT NULL CHECK (source IN ('miniapp')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
