-- vNext WP0c specification only. DO NOT execute without the P7 user gate.
-- Local S2/S3/S4 remain the source of truth; this is a serving projection.

CREATE TABLE IF NOT EXISTS building_images (
    canonical_bld_id   TEXT NOT NULL
        REFERENCES canonical_v2_buildings(canonical_bld_id) ON DELETE CASCADE,
    image_asset_id     TEXT NOT NULL CHECK (length(trim(image_asset_id)) > 0),
    url                TEXT NOT NULL CHECK (length(trim(url)) > 0),
    sort_order         INTEGER NOT NULL CHECK (sort_order >= 0),

    -- Arrays are intentional: semantic meaning and cover membership are non-exclusive.
    semantic_type      TEXT[] NOT NULL CHECK (
        cardinality(semantic_type) >= 1 AND
        semantic_type <@ ARRAY[
            'exterior_overall','exterior_context','interior','threshold',
            'drawing','rendering','physical_model','aerial','detail',
            'construction_archive'
        ]::TEXT[]
    ),
    medium             TEXT NOT NULL CHECK (medium IN (
        'photograph','drawing','rendering','physical_model','mixed','other','unknown'
    )),
    spatial_context    TEXT CHECK (spatial_context IN ('exterior','interior','threshold','unknown')),
    framing_scale      TEXT CHECK (framing_scale IN (
        'site_context','overall','element_detail','material_detail','unknown'
    )),
    camera_angle       TEXT CHECK (camera_angle IN (
        'eye_level','elevated','aerial_oblique','aerial_top_down','unknown'
    )),
    drawing_kind       TEXT CHECK (drawing_kind IN (
        'plan','site_plan','section','elevation','axonometric','perspective',
        'detail','diagram','sketch','composite','other','unknown'
    )),
    project_state      TEXT CHECK (project_state IN (
        'visibly_finished','construction_visible','ruin_or_abandoned_visible',
        'demolition_visible','unknown'
    )),
    project_legibility TEXT NOT NULL CHECK (project_legibility IN ('high','medium','low','unknown')),

    is_cover_default   BOOLEAN NOT NULL DEFAULT FALSE,
    cover_slot         TEXT[] NOT NULL DEFAULT '{}'::TEXT[] CHECK (
        cover_slot <@ ARRAY['exterior','interior','drawing','aerial','detail']::TEXT[]
    ),
    engine             TEXT NOT NULL CHECK (length(trim(engine)) > 0),
    engine_version     TEXT NOT NULL CHECK (length(trim(engine_version)) > 0),
    projection_version TEXT NOT NULL CHECK (length(trim(projection_version)) > 0),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (canonical_bld_id, image_asset_id),
    UNIQUE (canonical_bld_id, sort_order),

    CHECK (
        (medium = 'photograph' AND spatial_context IS NOT NULL
            AND framing_scale IS NOT NULL AND camera_angle IS NOT NULL
            AND drawing_kind IS NULL AND project_state IS NOT NULL)
        OR
        (medium = 'drawing' AND spatial_context IS NULL
            AND framing_scale IS NULL AND camera_angle IS NULL
            AND drawing_kind IS NOT NULL AND project_state IS NULL)
        OR
        (medium = 'rendering' AND spatial_context IS NOT NULL
            AND framing_scale IS NOT NULL AND camera_angle IS NOT NULL
            AND drawing_kind IS NOT NULL AND project_state IS NULL)
        OR
        (medium IN ('physical_model','mixed','other','unknown')
            AND spatial_context IS NULL AND framing_scale IS NULL
            AND camera_angle IS NULL AND drawing_kind IS NULL
            AND project_state IS NULL)
    ),
    CHECK (
        camera_angle NOT IN ('aerial_oblique','aerial_top_down')
        OR spatial_context IN ('exterior','unknown')
    ),
    CHECK (NOT is_cover_default OR medium = 'photograph'),
    CHECK (
        NOT (cover_slot && ARRAY['exterior','interior','aerial','detail']::TEXT[])
        OR medium = 'photograph'
    ),
    CHECK (NOT (cover_slot @> ARRAY['drawing']::TEXT[]) OR medium = 'drawing')
);

CREATE INDEX IF NOT EXISTS idx_building_images_semantic_type
    ON building_images USING GIN (semantic_type);
CREATE INDEX IF NOT EXISTS idx_building_images_cover_slot
    ON building_images USING GIN (cover_slot);

-- At most one selected image per building and slot. One row may serve several slots.
CREATE UNIQUE INDEX IF NOT EXISTS uq_building_images_default_cover
    ON building_images (canonical_bld_id) WHERE is_cover_default;
CREATE UNIQUE INDEX IF NOT EXISTS uq_building_images_exterior_cover
    ON building_images (canonical_bld_id) WHERE cover_slot @> ARRAY['exterior']::TEXT[];
CREATE UNIQUE INDEX IF NOT EXISTS uq_building_images_interior_cover
    ON building_images (canonical_bld_id) WHERE cover_slot @> ARRAY['interior']::TEXT[];
CREATE UNIQUE INDEX IF NOT EXISTS uq_building_images_drawing_cover
    ON building_images (canonical_bld_id) WHERE cover_slot @> ARRAY['drawing']::TEXT[];
CREATE UNIQUE INDEX IF NOT EXISTS uq_building_images_aerial_cover
    ON building_images (canonical_bld_id) WHERE cover_slot @> ARRAY['aerial']::TEXT[];
CREATE UNIQUE INDEX IF NOT EXISTS uq_building_images_detail_cover
    ON building_images (canonical_bld_id) WHERE cover_slot @> ARRAY['detail']::TEXT[];
