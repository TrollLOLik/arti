CREATE TABLE arti_projects (
    id TEXT PRIMARY KEY,realm TEXT NOT NULL,scope_key TEXT NOT NULL,scope JSONB NOT NULL,
    owner_id BIGINT NOT NULL,status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted')),
    revision BIGINT NOT NULL DEFAULT 1,access_generation BIGINT NOT NULL DEFAULT 0,
    payload JSONB,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX arti_project_realm ON arti_projects(realm,status);
CREATE TABLE arti_project_members (
    project_id TEXT NOT NULL REFERENCES arti_projects(id),user_id BIGINT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','manager','editor','contributor','viewer','approver')),
    PRIMARY KEY(project_id,user_id)
);
CREATE TABLE arti_project_revisions (
    project_id TEXT NOT NULL REFERENCES arti_projects(id),revision BIGINT NOT NULL,
    actor_id BIGINT NOT NULL,kind TEXT NOT NULL,payload JSONB,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(project_id,revision)
);
CREATE TABLE arti_project_materials (
    project_id TEXT NOT NULL REFERENCES arti_projects(id),asset_id TEXT NOT NULL REFERENCES material_assets(id),
    version INTEGER NOT NULL,added_by BIGINT NOT NULL,PRIMARY KEY(project_id,asset_id)
);
CREATE TABLE arti_project_results (
    project_id TEXT NOT NULL REFERENCES arti_projects(id),result_key TEXT NOT NULL,
    derivative_id TEXT NOT NULL REFERENCES material_derivatives(id),accepted_by BIGINT,
    PRIMARY KEY(project_id,result_key)
);
CREATE TABLE arti_project_selections (
    realm TEXT NOT NULL,user_id BIGINT NOT NULL,project_id TEXT NOT NULL REFERENCES arti_projects(id),
    PRIMARY KEY(realm,user_id)
);
