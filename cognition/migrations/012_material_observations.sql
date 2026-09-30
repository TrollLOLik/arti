-- Confirmed timed observations keep originals and immutable correction history.
CREATE TABLE material_observation_heads (
    realm TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES material_assets(id),
    kind TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES material_derivatives(id),
    revision BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY(realm,asset_id,kind)
);
