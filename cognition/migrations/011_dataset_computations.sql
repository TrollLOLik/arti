-- Immutable dataset/computation payloads reuse the scoped material lifecycle.
ALTER TABLE material_derivatives ADD COLUMN realm TEXT;
ALTER TABLE material_derivatives ADD COLUMN sha256 TEXT;
CREATE TABLE material_dataset_heads (
    realm TEXT NOT NULL, series_key TEXT NOT NULL,
    dataset_id TEXT NOT NULL REFERENCES material_derivatives(id),
    revision BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY(realm,series_key)
);
CREATE TABLE material_derivative_links (
    derivative_id TEXT NOT NULL REFERENCES material_derivatives(id),
    input_id TEXT NOT NULL REFERENCES material_derivatives(id),
    PRIMARY KEY(derivative_id,input_id), CHECK(derivative_id<>input_id)
);
CREATE INDEX material_derivative_links_input ON material_derivative_links(input_id);
