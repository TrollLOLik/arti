"""Idempotent cutover from proxy aliases to database-owned API connections."""


async def migrate_ai_models(conn):
    # All changes and selection rewrites commit together. Existing catalog rows,
    # disabled flags and unknown selections are retained.
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(731903210)")
        await conn.execute("""
            ALTER TABLE ai_models
                ADD COLUMN IF NOT EXISTS base_url TEXT,
                ADD COLUMN IF NOT EXISTS api_key TEXT,
                ADD COLUMN IF NOT EXISTS api_type TEXT NOT NULL DEFAULT 'openai',
                ADD COLUMN IF NOT EXISTS request_options JSONB NOT NULL DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS fast_request_options JSONB NOT NULL DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS capabilities JSONB NOT NULL
                    DEFAULT '{"inputs":["text"],"features":[]}'::jsonb,
                ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER NOT NULL DEFAULT 60;
            ALTER TABLE chat_models ALTER COLUMN model_id SET DEFAULT 'gemini';
            WITH unique_models AS (
                SELECT model, min(key) AS key FROM ai_models
                GROUP BY model HAVING count(*) = 1
            )
            UPDATE chat_models c SET model_id = m.key
            FROM unique_models m
            WHERE c.model_id = m.model
                AND NOT EXISTS (SELECT 1 FROM ai_models k WHERE k.key = c.model_id);
            COMMENT ON COLUMN ai_models.model IS 'Exact upstream model ID, without a router alias';
            COMMENT ON COLUMN ai_models.base_url IS 'OpenAI-compatible API root, including /v1 if required';
            COMMENT ON COLUMN ai_models.api_key IS 'Private bearer credential; NULL means unconfigured, empty string means explicit anonymous API';
            COMMENT ON COLUMN ai_models.api_type IS 'Request protocol: openai or native gemini (google-genai)';
            COMMENT ON COLUMN chat_models.model_id IS 'Stable ai_models.key; legacy IDs resolve only when unique';
            CREATE OR REPLACE FUNCTION reset_ai_model_capability_evidence() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                IF (NEW.base_url IS DISTINCT FROM OLD.base_url OR NEW.model IS DISTINCT FROM OLD.model
                    OR NEW.api_type IS DISTINCT FROM OLD.api_type)
                    AND NEW.capabilities->>'evidence' IN ('metadata','probe')
                    AND NEW.capabilities->>'observed_at' IS NOT DISTINCT FROM OLD.capabilities->>'observed_at'
                THEN
                    NEW.capabilities := '{"inputs":["text"],"features":[]}'::jsonb;
                END IF;
                RETURN NEW;
            END $$;
            DROP TRIGGER IF EXISTS ai_model_capability_evidence ON ai_models;
            CREATE TRIGGER ai_model_capability_evidence BEFORE UPDATE OF base_url,model,api_type ON ai_models
                FOR EACH ROW EXECUTE FUNCTION reset_ai_model_capability_evidence();
        """)
