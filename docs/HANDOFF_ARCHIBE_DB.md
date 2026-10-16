# Archibe(archi-tinder) DB 핸드오프 — make_db ↔ 서비스

| | |
|---|---|
| 기준일 | 2026-10-16 (라이브 Neon read-only 확인) |
| 대상 | ksangjo (archi-tinder 백엔드·추천 알고리즘), owner (make_db·vNext) |
| 데이터 릴리스 | `completeness_c27_cover2026q3` (c26_rem2026q2 + 커버 재선정 145건, 2026-07-06) |
| 스키마 스냅샷 | `docs/schema/archi_data.schema.json` (생성: `tools/neon_schema_snapshot.py`) · 변경 이력 `docs/schema/CHANGELOG.md` |

목표는 하나다: **dev와 prod DB가 조용히 어긋나지 않게 한다.** 아래 §8 규칙이 이 문서의 핵심이다.

---

## 1. 시스템 구성

```
 [make_db 리포 (이 리포, owner 단독)]
   crawl → enrich(D-1/D-2) → canonical 아티팩트(c27, 로컬 불변 파일)
        │
        │  tools/canonical_v2_neon_loader.py --upsert --confirm-db-write   (owner만, user-gated)
        │  tools/canonical_v2_architects_neon_loader.py                     (owner만)
        │  tools/canonical_v2_tag_stats_build.py --with-r4                  (owner만)
        ▼
 ┌──────────────────────── Neon project (ap-southeast-1) ────────────────────────┐
 │  branch: production (endpoint ep-broad-hat-a1jaomn7)                          │
 │    DB archi_data  ── public.canonical_v2_buildings / _architects / _tag_*     │
 │                      (pgvector 0.8.0)      writer: neondb_owner              │
 │                                            reader: make_web (SELECT only)    │
 │    DB user_data   ── Django auth/profiles/swipes (archi-tinder 마이그레이션)   │
 │                                            app role: make_web_app            │
 │                                                                               │
 │  branch: dev-ksangjo (production child, endpoint ep-weathered-base-a1xc6frq)  │
 │    archi_data  ── public = prod 복사본, + schema vnext_preview(mock)          │
 │    user_data   ── prod 복사본 (마이그레이션 실험 가능)                          │
 └───────────────────────────────────────────────────────────────────────────────┘
        ▲                                   ▲
        │ BUILDINGS_DB_* (make_web, RO)     │ DB_* (make_web_app)
 [archi-tinder Django — Railway, main 자동 배포, 마이그레이션은 수동 make migrate-prod]
        ▲
 [archi-tinder 프론트 — Vite on Vercel]
        │ 이미지: display_cover_url / all_images[].url → 소스 CDN 직접 (R2 미사용)
```

## 2. 소유권

| 대상 | 스키마 소유 | 데이터 쓰기 | 비고 |
|---|---|---|---|
| `archi_data.public.*` (5개 테이블) | **make_db (owner)** — 로더 DDL | **owner의 로더만** | 서비스는 SELECT 전용(`make_web`). 서비스 쪽 Django router가 마이그레이션 차단 |
| `archi_data.vnext_preview.*` | make_db (owner) | owner의 seed 스크립트 (dev 브랜치에만 존재) | 실험/사전공개용. prod에는 없음 |
| `user_data.*` | **archi-tinder Django migrations** | 서비스 런타임(`make_web_app`) | 스키마 변경은 PR의 마이그레이션 파일로만 |

- make_db는 `user_data`를 건드리지 않는다. archi-tinder는 `archi_data`에 쓰지 않는다.
- 데이터 품질·어휘·ID 문제를 발견하면 archi_data를 직접 고치지 말고 owner에게 이슈로 전달한다.

## 3. 환경

| 환경 | Neon 위치 | archi_data | user_data | 누가 쓰나 | 리셋 |
|---|---|---|---|---|---|
| **prod** | branch `production`, endpoint `ep-broad-hat-a1jaomn7` | 실 데이터, owner 로더만 쓰기 | Django prod (Railway) | 서비스 운영 | — |
| **dev-ksangjo** | branch `dev-ksangjo` (production child, endpoint `ep-weathered-base-a1xc6frq`, 2026-10-16 생성, 자동삭제 없음) | public = prod 복사 + `vnext_preview` (mock) | prod 복사, 마이그레이션 실험 | ksangjo 로컬/프리뷰 | prod에서 reset (§8-2) |
| **local** | 개발자 PC의 Django → dev-ksangjo 브랜치 접속 | 위와 동일 | 위와 동일 | 개발 | — |

로컬 셋업 절차: archi-tinder 리포 `docs/runbooks/local-setup.md`.

## 4. 접속 (변수 이름만 — 값은 Neon 콘솔/보안 채널로 받는다)

**archi-tinder (서비스)**
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` → Django `default` = `user_data` (role `make_web_app`)
- `BUILDINGS_DB_HOST`, `BUILDINGS_DB_NAME`, `BUILDINGS_DB_USER`, `BUILDINGS_DB_PASSWORD`, `BUILDINGS_DB_SSLMODE` (+ 필요시 포트) → Django `buildings` = `archi_data` (role `make_web`, read-only; router가 migrate 차단)
- dev 값은 owner가 주는 `backend.env.dev-ksangjo` 블록을 그대로 붙인다. **dev 브랜치의 비밀번호는 prod와 다르게 교체돼 있다** (prod 자격증명으로는 dev에 못 붙고, dev 자격증명으로는 prod에 못 붙는다). **prod 호스트를 로컬 `.env`에 두지 않는다.**
- `DEV_MIGRATE_DB_USER/PASSWORD` (dev 브랜치의 `neondb_owner`)는 `make migrate-local`처럼 dev `user_data`에 DDL이 필요할 때만 잠깐 `DB_USER/DB_PASSWORD` 자리에 쓴다.

**make_db (이 리포, owner)**
- `.env`: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSLMODE` (또는 `DATABASE_URL`) — writer `neondb_owner`. 템플릿 `.env.example`
- `.env.make-web`: `BUILDINGS_DB_*` (read-only). 템플릿 `.env.make-web.example`
- 접속 코드: `tools/canonical_v2_neon_loader.py._connect`. Windows는 `.venv/Scripts/python.exe` 사용(시스템 python에 psycopg2 없음).
- 읽기 전용 점검: `.venv/Scripts/python.exe tools/canonical_v2_neon_loader.py --inspect-table`

## 5. 데이터 사전 (archi_data.public, 2026-10-16 라이브)

규모: buildings **39,478행 / is_publishable 36,673** (45컬럼) · architects **14,216 / is_recommendable 4,348** (37컬럼) · tag_stats / tag_centroids / tag_vocabulary 각 **15,655행**, 11축, `corpus_version = c23_final+matstrip+r4+rem2026q2+prog2026q2`.

어휘 원본: `core/vocab.py` (`VOCAB_VERSION = "v2"`, 변경은 owner/사용자 결정). 요약은 `docs/REFERENCE.md` §3.

### 5.1 `canonical_v2_buildings` (PK `canonical_bld_id`)

| 컬럼 | 타입 | 의미 | NULL/빈값 의미 · 어휘 |
|---|---|---|---|
| `canonical_bld_id` | text NOT NULL PK | 안정 건물 ID (`data/id_registry.json`) | 스와이프가 참조하는 키 |
| `name` | text NOT NULL | 대표 이름 | |
| `names_alts` | text[] NOT NULL `{}` | 별칭 | 빈 배열 = 별칭 없음 |
| `location_city` | text | 도시 | NULL = 미상 |
| `location_country` | text | 국가 | NULL = 미상 (publishable 아님: `missing_country`) |
| `project_year` | integer | 준공/프로젝트 연도 | NULL = 미상 |
| `architect_canonical_ids` | text[] NOT NULL | → `canonical_v2_architects.canonical_arch_id` | 빈 배열 = 연결 없음 |
| `architect_names` | text[] NOT NULL | 표시용 설계자 이름 | |
| `architects_text` | text | 원문 크레딧 문자열 | |
| `program` | text NOT NULL | 용도(대분류) | 14개 controlled vocab |
| `style` | text NOT NULL | 양식 | 12개 vocab, vocab-clean |
| `color_tone` | text NOT NULL | 색조 | 8개 vocab |
| `atmosphere` | text NOT NULL | 분위기 | 12개 vocab |
| `material_visual` | text[] NOT NULL | 재료 (lowercase 자유 텍스트) | 비통제 — 롱테일 많음, 태그 테이블에서 `doc_freq`로 필터 |
| `visual_description` | text NOT NULL | 텍스트 기반 시각 묘사(D-1) | 임베딩 입력 |
| `image_derived` | jsonb NOT NULL `{}` | D-2 커버 비전 결과 `{style,color_tone,material_visual,visual_description}` | **advisory** — 약 24% 어휘 밖. 필터에 쓰지 말 것 |
| `covers_by_type` | jsonb NOT NULL | `{exterior,interior,drawing,aerial,detail}` → URL | 5개 키 항상 존재, 없는 슬롯은 JSON `null` |
| `all_images` | jsonb NOT NULL `[]` | 이미지 배열. 원소 키: `url, source, source_id, kind(cover/gallery), rank, image_order, type, w, h, bytes, phash, phash_cluster_id` | w/h/bytes/type 상당수 null |
| `best_image_per_cluster` | jsonb NOT NULL `{}` | pHash 클러스터 id(문자열) → 대표 이미지 객체 | |
| `source_refs` | jsonb NOT NULL | `{source: [source ids]}` | divisare/architizer/archello/metalocus |
| `source_urls` | jsonb NOT NULL `{}` | `{source: [urls]}` | |
| `identity_source` | text | 대표 정체성 소스 (divisare/architizer/archello) | |
| `confidence_tier` | text NOT NULL | T1(≥3 소스)/T2(2)/T3(1) | CHECK |
| `n_sources` | integer NOT NULL | 소스 수 (≥1) | |
| `cover_image_url_default` | text | 파이프라인 원래 기본 커버 | 구값. **서빙은 `display_cover_url` 사용** |
| `display_cover_url` | text | **서빙용 커버 URL** (소스 CDN 직링크) | 203행 NULL. architizer-prod.imgix.net 커버는 HTTP 410(§7) |
| `cover_image_cdn_url` | text | R2 재호스팅 URL | 전부 NULL (R2 미사용) |
| `cover_blurhash` | text | 블러해시 | 미채움 |
| `is_publishable` | boolean NOT NULL false | **서비스 노출 가능 여부** | 서비스 쿼리는 항상 `WHERE is_publishable` |
| `publishability_reasons` | text[] NOT NULL | 비공개 사유 (missing_country, seo_or_generic_title, material_noise_only, image_unavailable, …) | 빈 배열 = 사유 없음 |
| `needs_image_derived_backfill` | boolean NOT NULL | D-2 재실행 필요 플래그 | 현재 0행 true, advisory |
| `embedding` | vector(384) NOT NULL | 텍스트 임베딩 (`paraphrase-multilingual-MiniLM-L12-v2`) | **정규화 안 됨** → cosine `<=>` 사용. HNSW `vector_cosine_ops` |
| `created_at` / `updated_at` | timestamptz NOT NULL | 생성/수정 | **updated_at은 유지되지 않음**(max 2026-05-23). 변경 감지에 쓰지 말 것 |
| `typology_primary` | text | 세부 용도 1개 | 35개 typology vocab, NULL = 미결 (약 2.6k) |
| `typology_primary_source` | text | 유도 경로: source_tags / llm_rederive_2026q2 / program / descr_rederive_2026q2 / name / typ_fix_prog_contra_2026q2 | |
| `typology_tags` | text[] NOT NULL | 세부 용도 다중 | typology vocab |
| `architectural_elements` | text[] NOT NULL | 요소 (Stair, Facade, Courtyard …) | 14개 vocab |
| `source_categories` | jsonb NOT NULL `{}` | 소스 원 분류 태그 `{source: [slugs]}` | 원문 그대로, 비통제 |
| `year_kind` | text NOT NULL `'unknown'` | completed / future / unknown | project_year 기준 |
| `era` | text | R4: Pre-1900 / 1900-1945 / 1945-1980 / 1980-2000 / 2000-2015 / 2015+ | **NULL = 미해결** (‘Unknown’ 문자열 없음) |
| `scale` | text | R4: XS/S/M/L/XL | NULL = 미해결 |
| `structural_system` | text | R4: Masonry / Reinforced Concrete / Steel Frame / Timber Frame / Hybrid / Shell/Membrane / Earth | NULL = 미해결 |
| `roof_type` | text | R4: Flat / Gabled / Hipped / Shed / Curved / Green Roof / Vaulted/Domed / Sawtooth | NULL = 미해결 (publishable 중 **63%만 채워짐**) |
| `facade_pattern` | text | R4: Grid / Louvered / Solid/Mass / Glazed Curtain / Perforated / Organic / Layered / Rhythmic Openings | NULL = 미해결 |

인덱스: B-tree(country/city/year/program/style/tier/is_publishable/typology_primary/year_kind/R4 5축), GIN(architect_canonical_ids/source_refs/typology_tags/architectural_elements/source_categories), HNSW(embedding, cosine). 권위 DDL: `tools/canonical_v2_neon_loader.py --emit-sql`.

### 5.2 `canonical_v2_architects` (PK `canonical_arch_id`)

| 컬럼 | 타입 | 의미 | NULL/비고 |
|---|---|---|---|
| `canonical_arch_id` | text PK | `arch_NNNNNN` (`data/id_registry_architects.json`) | |
| `canonical_name` | text NOT NULL | 표시명 | |
| `name_alts` | text[] NOT NULL | 별칭 | |
| `description` | text | 소개 | NULL 가능 |
| `primary_country` / `primary_city` | text | 포트폴리오 최빈 국가/도시 | NULL = 미상 |
| `office_locations` | jsonb NOT NULL `[]` | `[{city,country,address,is_hq}]` | |
| `website` | text | https 정규화 | |
| `email` | text | — | **항상 NULL** (소스에 없음) |
| `phone` | text | 전화 | |
| `social_links` | jsonb NOT NULL `{}` | `{instagram,facebook,linkedin,…}` | |
| `building_ids` | text[] NOT NULL | → buildings PK | |
| `n_buildings` / `n_buildings_publishable` | integer NOT NULL | 포트폴리오 규모 | |
| `countries` / `cities` | text[] NOT NULL | 포트폴리오 전체 위치 | |
| `top_programs/styles/color_tones/atmospheres/materials/typologies/arch_elements` | text[] NOT NULL | 축별 top-5 빈도 | |
| `feature_distribution` | jsonb NOT NULL | 축별 전체 카운터 (program/style/color_tone/atmosphere/material_visual/typologies/architectural_elements) | |
| `earliest_project_year` / `latest_project_year` | integer | 연도 범위 | NULL 가능 |
| `source_refs` | jsonb NOT NULL | 소스별 firm id | |
| `source_urls` / `source_descriptions` | jsonb NOT NULL | 소스별 프로필 URL·소개 | |
| `n_sources` | integer NOT NULL | ≥1 | |
| `confidence_tier` | text NOT NULL | T1/T2/T3 | |
| `logo_url` | text | archello 로고 | 약 26% 보유 (3,685) |
| `hero_building_id` | text | 대표 미리보기 건물 | |
| `portfolio_embedding` | vector(384) NOT NULL | publishable 건물 embedding 평균 — 건물과 같은 공간 | cosine `<=>`, HNSW |
| `is_recommendable` | boolean NOT NULL | `n_buildings_publishable ≥ 3` AND 메타 보유 | 추천 쿼리는 항상 이 필터 |
| `created_at` / `updated_at` | timestamptz | | updated_at 신뢰하지 말 것 |

추천 알고리즘·SQL: `docs/ARCHITECT_RECOMMENDATION.md`.

### 5.3 태그 3형제 (PK `(axis, tag)`, publishable 건물만 집계)

| 테이블 | 컬럼 |
|---|---|
| `canonical_v2_tag_stats` | `axis` text, `tag` text, `doc_freq` int(≥1), `total_n` int(=36,673), `idf` double (`ln(total_n/(doc_freq+1))`), `corpus_version` text, `computed_at` timestamptz |
| `canonical_v2_tag_centroids` | `axis`, `tag`, `centroid` vector(384) **L2 정규화**, `n_buildings` int(=doc_freq), `corpus_version`, `computed_at` |
| `canonical_v2_tag_vocabulary` | `axis`, `tag`, `display_ko` text(NULL=롱테일 미라벨), `display_en` text, `is_generic` bool, `sort_rank` int |

- `axis` ∈ program, style, color_tone, atmosphere, material_visual, architectural_elements, era, scale, structural_system, roof_type, facade_pattern (CHECK).
- 조인 계약: stats 키 == centroids 키, stats ⊆ vocabulary → vocabulary는 LEFT JOIN.
- material_visual은 15,561 태그(대부분 singleton) → `doc_freq >= 5` 등으로 필터.
- R4 축은 `sum(doc_freq)` = non-NULL 행 수 (≠ total_n). roof_type 23,160 / 36,673.
- 계약 상세: `docs/MAKEDB_ALGO_SUPPORT_RESPONSE.md`.

## 6. 샘플 SQL

```sql
-- (1) 서비스 노출 후보: 항상 publishable + 커버 있음
SELECT canonical_bld_id, name, location_country, project_year, program, style,
       display_cover_url
FROM canonical_v2_buildings
WHERE is_publishable AND display_cover_url IS NOT NULL
  AND display_cover_url NOT LIKE '%architizer-prod.imgix.net%'   -- 410 회피(§7)
LIMIT 50;

-- (2) cosine kNN: 좋아요한 건물과 비슷한 건물 (embedding 비정규화 → <=> 사용)
SELECT b.canonical_bld_id, b.name, 1 - (b.embedding <=> q.embedding) AS sim
FROM canonical_v2_buildings b,
     (SELECT embedding FROM canonical_v2_buildings WHERE canonical_bld_id = %(seed)s) q
WHERE b.is_publishable AND b.canonical_bld_id <> %(seed)s
ORDER BY b.embedding <=> q.embedding
LIMIT 20;
-- 사용자 벡터: '[v1,...,v384]'::vector 로 넘기고 동일하게 ORDER BY embedding <=> %(uv)s::vector

-- (3) 태그 테이블: 부트 로드 (material 롱테일 컷)
SELECT s.axis, s.tag, s.doc_freq, s.idf, v.display_ko, v.display_en, v.is_generic, v.sort_rank
FROM canonical_v2_tag_stats s
LEFT JOIN canonical_v2_tag_vocabulary v USING (axis, tag)
WHERE s.axis <> 'material_visual' OR s.doc_freq >= 5;

-- (4) 질문 답 → 태그 centroid로 후보 검색
SELECT b.canonical_bld_id, b.name
FROM canonical_v2_buildings b, canonical_v2_tag_centroids c
WHERE c.axis = 'style' AND c.tag = 'Brutalist' AND b.is_publishable
ORDER BY b.embedding <=> c.centroid
LIMIT 20;

-- (5) R4 축 필터 — NULL은 '모름', 가짜 값으로 COALESCE 금지
SELECT canonical_bld_id, roof_type FROM canonical_v2_buildings
WHERE is_publishable AND roof_type = 'Gabled';

-- (6) 건축사무소 추천 (사용자 벡터 기준)
SELECT canonical_arch_id, canonical_name, primary_country, hero_building_id,
       1 - (portfolio_embedding <=> %(uv)s::vector) AS similarity
FROM canonical_v2_architects
WHERE is_recommendable AND canonical_arch_id <> ALL(%(exclude_ids)s)
ORDER BY portfolio_embedding <=> %(uv)s::vector
LIMIT 10;

-- (7) 신선도 확인
SELECT DISTINCT corpus_version, max(computed_at) OVER () FROM canonical_v2_tag_stats LIMIT 1;
```

## 7. 주의사항 (Caveats)

| # | 내용 | 대응 |
|---|---|---|
| C1 | `embedding`은 **정규화되지 않음** (centroid만 정규화) | cosine `<=>` 사용, 내적 쓰려면 직접 정규화 |
| C2 | R4 5축 NULL = 미해결. roof_type은 publishable의 63%만 | NULL을 "없음"으로 해석·대체하지 말 것 |
| C3 | `material_visual` 자유 텍스트, 롱테일 | `doc_freq` 필터 / `n_buildings` 가중 |
| C4 | `image_derived` 약 24% 어휘 밖 | advisory, 필터·매칭 키로 쓰지 말 것 |
| C5 | `updated_at` 미유지 (max 2026-05-23) | 변경 감지는 `corpus_version`·CHANGELOG로 |
| C6 | 이미지 URL은 소스 CDN 직링크. R2 미사용, `cover_image_cdn_url` 전부 NULL | 프론트에서 로드 실패 fallback 필요 |
| C7 | **architizer-prod.imgix.net 커버 → HTTP 410 `disabled_source`** (publishable 7,375행) | 해당 URL 필터 또는 `all_images`의 다른 소스 이미지로 대체. 근본 수정은 vNext |
| C8 | `display_cover_url` NULL 203행 | 서빙 쿼리에 `display_cover_url IS NOT NULL` |
| C9 | 서빙 커버는 `display_cover_url` (`cover_image_url_default`와 대부분 다름) | 커버엔 항상 display_cover_url |
| C10 | `canonical_v2_architects.email` 항상 NULL | UI에 노출하지 말 것 |

## 8. DEV/PROD 일관성 규칙 (필수)

1. **prod `archi_data`는 owner의 로더만 쓴다.** (`canonical_v2_neon_loader.py`, `canonical_v2_architects_neon_loader.py`, `canonical_v2_tag_stats_build.py`). 서비스·수동 SQL·다른 스크립트로 prod archi_data에 DDL/DML 금지.
2. **dev 브랜치(`dev-ksangjo`)는 prod에서 reset (owner가 Neon 콘솔에서: Branches → dev-ksangjo → Reset from parent)** — make_db 릴리스 직후 + 최소 주 1회. ⚠ reset하면 role 비밀번호가 prod 값으로 돌아갈 수 있으므로, reset 직후 owner가 dev 비밀번호를 다시 교체하고 새 블록을 전달한다. reset은 브랜치 변경을 전부 버리므로, 실험 DDL은 반드시 마이그레이션/스크립트 파일로 리포에 두고 reset 후 재실행한다. `vnext_preview`도 reset 후 owner가 `tools/vnext_preview_seed.py --dsn-env <DEV_OWNER_URL_VAR> --apply`를 다시 돌린다 (prod 호스트는 도구가 거부, `make_web`에 SELECT 권한 자동 부여).
3. **`user_data` 스키마는 Django 마이그레이션(PR)으로만.** PR merge → Railway 자동 배포 직후 owner가 즉시 `make migrate-prod` (neondb_owner). dev는 `make migrate-local`. 콘솔에서 손으로 ALTER 금지.
4. **DB를 건드리는 PR merge 전, 그리고 매 릴리스 후 `tools/neon_schema_snapshot.py --dsn-env <DEV_URL_VAR> --compare docs/schema/archi_data.schema.json` 실행** (make_db 리포는 public이라 clone해서 실행 가능, `make_web` RO 계정으로 충분; exit 2 = drift). drift가 나오면 **멈춘다** — 원인 규명 전 merge/배포 금지.
5. **매 make_db 릴리스는 `docs/schema/archi_data.schema.json` + `docs/schema/CHANGELOG.md`를 갱신**한다 (행수, corpus_version, 컬럼 변경).
6. **릴리스 전 스키마는 `vnext_preview` 스키마에만** 둔다. public에 미리 테이블/컬럼을 만들지 않는다.

## 9. vNext 변경 예고 (P7 배포 시점, 일정 미정)

- **`canonical_v2_buildings` 컬럼 모양은 그대로.** 값만 vNext 파이프라인으로 재생성 (`docs/vnext/PROJECTION_SPEC.md`).
- **신규 테이블 `building_images`** (DDL `docs/vnext/BUILDING_IMAGES_DDL.sql`): `canonical_bld_id` FK(ON DELETE CASCADE), `image_asset_id`, `url`, `sort_order`(건물별 0부터 연속), `semantic_type[]`, `medium`, 시점·프레이밍 필드, `is_cover_default`, `cover_slot[]`, `engine/engine_version/projection_version`.
- **ID 규칙:** legacy와 1:1이 명확하면 기존 `canonical_bld_id` 유지, 아니면 신규 ID (`docs/vnext/CONTRACT_IDENTITY.md` §6). → **스와이프 ID가 orphan될 수 있음. 현재 remap 계획 없음** — swipe 조인은 LEFT JOIN + 누락 허용으로 작성.
- **R4 4축(scale/structural/roof/facade)은 신규·비1:1 행에서 NULL** (era는 project_year로 재계산).
- **`covers_by_type`은 항상 5개 키** (exterior/interior/drawing/aerial/detail), 없는 슬롯은 `null`. default 커버가 없으면 `is_publishable=false`.
- URL은 계속 소스 CDN (R2 재호스팅 범위 밖).
- **dev 브랜치의 `vnext_preview.building_images`는 MOCK** (`engine='mock_preview'`, 현재 `all_images`/`covers_by_type`에서 파생; 2026-10-16 시드: publishable 36,673동 → **830,388행**, checker PASS). 실제 분류값(semantic_type 등)은 P7에서 들어온다. 테이블은 `vnext_preview` 스키마에 있으므로 쿼리에서 `vnext_preview.building_images`로 부르거나 search_path로 지정한다.
- **`vnext_preview` 대상 코드는 feature flag 뒤에서만.** prod 코드 경로가 플래그 없이 `vnext_preview`를 참조하면 안 된다 (prod에는 그 스키마가 없다).

## 10. ksangjo 시작 체크리스트

- [ ] owner에게 dev-ksangjo 브랜치 endpoint + `make_web`(RO) / `make_web_app` 자격증명을 보안 채널로 받기
- [ ] archi-tinder `docs/runbooks/local-setup.md`대로 로컬 셋업, `DB_*`·`BUILDINGS_DB_*` HOST가 **dev 브랜치**인지 확인 (prod 호스트 금지)
- [ ] `make migrate-local` 후 앱 기동, 샘플 SQL(§6) 몇 개 실행해 행수 확인 (39,478 / 36,673 / 14,216 / 4,348 / 15,655)
- [ ] §7 Caveats 확인 — 특히 C1(cosine), C2(R4 NULL), C7(architizer 410)
- [ ] PR #431(algo-taste-start), #437(algo-feedback, 마이그레이션 포함) 리베이스 시: 마이그레이션은 `user_data`에만, merge 후 owner가 `make migrate-prod`
- [ ] DB 관련 PR merge 전 `tools/neon_schema_snapshot.py --compare` 결과 첨부
- [ ] vNext 실험 코드는 feature flag + `vnext_preview` 스키마로만
- [ ] 데이터 이상(어휘·ID·이미지) 발견 시 archi_data 직접 수정 말고 owner에게 이슈
