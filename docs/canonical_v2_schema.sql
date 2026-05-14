-- canonical_v2_buildings schema
-- Production Neon DB, Singapore region
-- 39,776 rows as of 2026-05-14

CREATE EXTENSION IF NOT EXISTS vector;


CREATE TABLE IF NOT EXISTS canonical_v2_buildings (
    canonical_bld_id              TEXT        PRIMARY KEY,
    name                          TEXT        NOT NULL,
    names_alts                    TEXT[]      NOT NULL DEFAULT '{}',
    location_city                 TEXT,
    location_country              TEXT,
    project_year                  INTEGER,
    architect_canonical_ids       TEXT[]      NOT NULL DEFAULT '{}',
    architect_names               TEXT[]      NOT NULL DEFAULT '{}',
    architects_text               TEXT,
    program                       TEXT        NOT NULL,
    style                         TEXT        NOT NULL,
    color_tone                    TEXT        NOT NULL,
    atmosphere                    TEXT        NOT NULL,
    material_visual               TEXT[]      NOT NULL,
    visual_description            TEXT        NOT NULL,
    image_derived                 JSONB       NOT NULL DEFAULT '{}',
    covers_by_type                JSONB       NOT NULL,
    all_images                    JSONB       NOT NULL DEFAULT '[]',
    best_image_per_cluster        JSONB       NOT NULL DEFAULT '{}',
    source_refs                   JSONB       NOT NULL,
    source_urls                   JSONB       NOT NULL DEFAULT '{}',
    identity_source               TEXT,
    confidence_tier               TEXT        NOT NULL CHECK (confidence_tier IN ('T1','T2','T3')),
    n_sources                     INTEGER     NOT NULL,
    cover_image_url_default       TEXT,
    display_cover_url             TEXT,
    is_publishable                BOOLEAN     NOT NULL DEFAULT FALSE,
    publishability_reasons        TEXT[]      NOT NULL DEFAULT '{}',
    needs_image_derived_backfill  BOOLEAN     NOT NULL DEFAULT FALSE,
    embedding                     VECTOR(384) NOT NULL,
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_buildings_country  ON canonical_v2_buildings (location_country);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_buildings_year     ON canonical_v2_buildings (project_year);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_buildings_program  ON canonical_v2_buildings (program);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_buildings_style    ON canonical_v2_buildings (style);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_buildings_tier     ON canonical_v2_buildings (confidence_tier);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_buildings_publish  ON canonical_v2_buildings (is_publishable);
CREATE INDEX IF NOT EXISTS idx_canonical_v2_buildings_arch_ids ON canonical_v2_buildings USING GIN (architect_canonical_ids);
