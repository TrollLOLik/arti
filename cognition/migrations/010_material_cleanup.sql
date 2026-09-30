-- Cross-store deletion must survive a crash between erasure and cognition rebuild.
CREATE TABLE material_cognitive_cleanup (
    asset_id TEXT PRIMARY KEY REFERENCES material_assets(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
