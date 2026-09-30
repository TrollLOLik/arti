-- Additive source store. Tombstones remain after content erasure.
CREATE TABLE material_blob_reservations (
    id TEXT PRIMARY KEY, realm TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE material_blobs (
    id TEXT PRIMARY KEY, realm TEXT NOT NULL, sha256 TEXT NOT NULL,
    byte_size BIGINT NOT NULL CHECK(byte_size>0), mime TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(realm,sha256)
);
CREATE TABLE material_assets (
    id TEXT PRIMARY KEY, realm TEXT NOT NULL, scope_key TEXT NOT NULL, identity_key TEXT NOT NULL,
    scope JSONB NOT NULL, owner_id BIGINT, sender_ref TEXT NOT NULL,
    source_id TEXT NOT NULL, source_key TEXT NOT NULL, filename TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1 CHECK(current_version>0),
    generation BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL, erased_at TIMESTAMPTZ,
    UNIQUE(realm,source_key)
);
CREATE INDEX material_assets_source ON material_assets(scope_key,source_id,owner_id);
CREATE INDEX material_assets_expiration ON material_assets(expires_at) WHERE erased_at IS NULL;
CREATE TABLE material_asset_versions (
    asset_id TEXT NOT NULL REFERENCES material_assets(id), version INTEGER NOT NULL,
    blob_id TEXT REFERENCES material_blobs(id), sha256 TEXT NOT NULL,
    byte_size BIGINT NOT NULL, mime TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(asset_id,version)
);
CREATE TABLE material_extractions (
    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, asset_version INTEGER NOT NULL,
    extractor TEXT NOT NULL, payload JSONB, sha256 TEXT NOT NULL,
    generation BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY(asset_id,asset_version) REFERENCES material_asset_versions(asset_id,version),
    UNIQUE(asset_id,asset_version,extractor)
);
CREATE TABLE material_derivatives (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload JSONB, invalidated_at TIMESTAMPTZ
);
CREATE TABLE material_dependencies (
    derivative_id TEXT NOT NULL REFERENCES material_derivatives(id),
    asset_id TEXT NOT NULL REFERENCES material_assets(id),
    PRIMARY KEY(derivative_id,asset_id)
);
CREATE TABLE material_source_tombstones (
    scope_key TEXT NOT NULL, owner_id BIGINT NOT NULL, source_id TEXT NOT NULL,
    erased_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(scope_key,owner_id,source_id)
);
