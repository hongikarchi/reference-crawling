# Codex prompt: Architizer source-specific curation

아래 내용을 다른 PC의 Codex 5.6 sol 세션에 그대로 전달한다.

```text
너는 reference-crawling 저장소에서 Architizer만을 위한
source-specific curated SQLite v1을 설계, 구현, 검증한다.

현재 다른 PC에서는 Divisare HTML recrawl이 실행 중이다. Divisare
파일, DB, snapshot, report에는 손대지 않는다. 계획만 제시하고 멈추지
말고, 입력이 준비되어 있으면 audit부터 full artifact와 Git push까지
자율적으로 완료한다.

[시작 전]

1. AGENTS.md, CLAUDE.md, docs/REFERENCE.md,
   docs/HANDOFF_20260731_PARALLEL_SOURCE_CURATION.md를 먼저 읽는다.
2. git fetch --tags origin, git switch main,
   git pull --ff-only origin main을 실행한다.
3. `git merge-base --is-ancestor handoff-divisare-20260731 HEAD`가
   성공하는지 확인한다.
4. 기존 패턴을 이해하기 위해 아래 파일을 읽되 수정하지 않는다.
   - crawl/architizer/**
   - canonical/_source_loaders.py
   - canonical/divisare_curated.py
   - tools/build_divisare_curated.py
   - docs/DIVISARE_CURATED_DB.md
   - 관련 Divisare 테스트
5. 이 저장소는 feature branch 없이 main만 사용한다. 기존 사용자
   변경을 revert하지 않는다.

[입력 준비]

Dropbox migration bundle의 data/crawl/architizer.db를 이 worktree의
data/crawl/architizer.db로 복사한다. Dropbox 원본을 직접 열어 쓰지
말고 반드시 로컬 복사본을 사용한다.

Expected size:
90,918,912 bytes

Expected SHA-256:
35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985

입력이 없거나 hash가 다르면 사이트를 재크롤링하지 않는다. 발견한
경로, 실제 크기/hash, 필요한 조치를 정확히 보고하고 그 지점에서
중단한다. 입력이 맞으면 builder는 SQLite mode=ro로 연다.

[목표]

기존 Architizer crawler SQLite를 read-only source of truth로 사용해
다른 사이트와 비교하기 전 단계의 독립적인 Architizer curated
SQLite를 만든다.

- project, architect/firm 관계, location, year, area, 원문 text,
  category/tag, image URL/occurrence를 provenance와 함께 보존한다.
- Architizer의 실제 schema와 데이터 의미를 먼저 조사한다.
- Divisare schema를 이름만 바꿔 복제하지 않는다.
- raw category/tag와 normalized program/typology/material/location
  claim을 분리한다.
- mapping에는 rule, confidence, evidence, status가 있어야 한다.
- 정보가 없거나 충돌하면 NULL, candidate 또는 review로 남긴다.
- project/source ID를 실제 building ID로 가정하지 않는다.
- strict internal duplicate cluster와 fuzzy review queue를 분리한다.
- 1 building당 1 row인 downstream export view와 provenance view를
  제공한다.
- builder는 deterministic하고 network, LLM, embedding, Neon, R2
  호출이 없어야 한다.

[1. Read-only audit]

구현 전에 다음을 조사하고 결과를 문서화한다.

- PRAGMA quick_check, integrity_check, foreign_key_check
- 모든 table/view/index와 column, PK, unique semantics
- 주요 entity row count와 crawl pending/failed 상태
- name, firm/architect, city/country, year, area, description coverage
- category/tag vocabulary, 빈도, hierarchy와 다중 값 구조
- built/unbuilt, concept, award/editorial 혼입 가능성
- cover/gallery/award image 관계, URL 구조와 중복 occurrence
- malformed JSON, mojibake, 잘린 text, 비정상 year/area, orphan 관계
- 내부 중복 후보의 name/firm/location/year 근거
- credit/team/award 중 identity/provenance에 필요한 최소 필드

확인하지 못한 source 의미는 추측하지 말고 open QA로 남긴다.

[2. 구현]

기본 산출물:

- canonical/architizer_curated.py
- tools/build_architizer_curated.py
- tests/test_architizer_curated.py
- docs/ARCHITIZER_CURATED_DB.md
- .claude/ops/jobs/20260731_architizer_curated_v1.md

필요하다면 테스트를 정책별로 여러 파일로 나눌 수 있다. 공통 파일은
수정하지 않는다.

Curated DB에는 실제 audit 결과에 맞춰 다음 개념을 구현한다.

- build lineage, input SHA, schema/policy version
- raw source project와 architect/firm membership
- raw category/tag와 project-category relation
- original text provenance
- raw image occurrence, normalized URL, asset identity
- typed attribute claim과 evidence/confidence/status
- provisional building과 project membership
- internal duplicate candidate/review queue
- normalized facet/array와 conflict 상태
- QA issue와 completeness metric
- source-specific building export view
- 향후 pHash/image classification을 위한 queue만 생성 가능

Program/typology는 검토된 Architizer category를 우선 근거로 쓴다.
text에서 LLM으로 값을 만들지 않는다. raw category는 mapping되지
않아도 모두 보존한다. article/category 수준의 drawing 힌트를 gallery
전체 이미지에 전파하지 않는다. credit payload 전체 복사는 제외하되
identity나 attribution에 필요한 최소 필드는 이유와 함께 보존한다.

[3. 내부 중복 정책]

- normalized name만으로 자동 merge하지 않는다.
- 자동 cluster는 name, stable firm/architect identity, location,
  동일한 non-null year 등 강한 결합 근거가 충족될 때만 허용한다.
- generic name, missing year, phase/sibling, award/editorial record는
  자동 merge하지 않는다.
- fuzzy name, 유사 주소/설명/이미지는 candidate evidence로만 쓴다.
- 모든 후보에 evidence와 score breakdown을 저장한다.
- pHash/Vision이 없음을 명시하고 membership provenance를 보존한다.

[4. Smoke ladder]

반드시 N10 -> N100 -> full 순서로 진행한다. 한 단계가 실패하면
원인을 수정하고 해당 단계를 다시 통과하기 전에는 다음으로 가지
않는다.

N10:
- deterministic subset과 versioned output/report
- 10건 row-level 직접 검토
- schema, FK, provenance, category, image occurrence, membership 확인
- 기존 artifact no-clobber와 입력 SHA 불변 확인

N100:
- category, completeness, image count, firm 유무가 다른 사례 포함
- edge case, orphan, duplicate assignment, silent default 검증
- 입력 SHA 불변과 deterministic rerun 확인

Full:
- N10/N100 통과 후에만 실행
- 입력은 mode=ro
- temporary DB에서 validation 후 immutable versioned path로 publish
- 기존 output을 덮어쓰지 않음

권장 output:
- data/curated/smoke/architizer_curated_n10_v1.db
- data/curated/smoke/architizer_curated_n100_v1.db
- data/curated/architizer_curated_v1.db
- 대응하는 data/reports/** Markdown report

[5. 필수 검증]

- integrity_check=ok, foreign key violation=0
- 입력 SHA가 build 전후 동일
- source project 누락/중복 accounting=0 또는 exclusion reason 존재
- accepted project당 provisional membership 정확히 1개
- raw category/tag occurrence 보존
- unmapped tag 삭제 0
- 근거 없는 confirmed/default facet 0
- scalar conflict는 abstain/review
- raw image occurrence 보존과 malformed URL count 보고
- gallery 전체에 article-level image type을 전파한 row 0
- auto cluster는 strict rule을 모두 충족
- fuzzy 후보는 merge되지 않고 review queue에 존재
- export building row unique
- deterministic rerun
- 정책 unit test와 실제 SQLite integration test 통과
- 기존 전체 테스트 회귀 없음

[금지 범위]

- Divisare 파일, DB, recrawl state, snapshot, report 수정
- core/vocab.py, canonical/schema.py, canonical/assemble_4source.py,
  canonical/_source_loaders.py, requirements*.txt 수정
- data/id_registry*.json 수정 또는 재생성
- cross-site matching, production canonical rebuild
- D1/D2/E2, LLM/Vision 호출
- image 전체 다운로드, pHash, image classification
- embedding/vector DB
- Neon/R2 접근 또는 production upload
- Architizer 사이트 재크롤링
- env/session/cookie/credential 출력 또는 commit
- git reset --hard, force-push, history rewrite
- git add ., git add -f data/**

[Git]

Git에는 code, test, docs, job card와 작은 policy 파일만 넣는다.
DB, WAL/SHM, report artifact, image cache, HTML, log, secret은 넣지 않는다.

완료 후:

1. git status와 diff를 검토한다.
2. 의도한 Architizer 파일만 명시적으로 stage한다.
3. git diff --cached --check와 테스트를 실행한다.
4. 논리 단위 commit을 만든다.
5. git pull --rebase origin main을 실행한다.
6. 충돌이 없고 테스트가 유지되면 git push origin main을 실행한다.
7. 충돌 시 공통 파일을 임의로 해결하지 말고 정확한 파일을 보고한다.

[완료 보고]

- 실제 raw schema와 audit 결과
- 구현 schema와 taxonomy/duplicate/image 정책
- 변경 파일
- N10, N100 validation
- full input/output SHA와 output 크기
- project/building/architect/category/image row count
- core field coverage
- confirmed/candidate/unmapped taxonomy count
- strict merge/fuzzy review 후보 수
- open QA 종류와 수
- integrity/FK/test 결과
- elapsed time
- network/API/LLM cost
- 알려진 한계와 다음 Architizer-only 단계
- commit SHA와 push 상태

입력 부재 또는 destructive/production 작업 외에는 합리적으로 조사해
결정하고 끝까지 진행한다. 애매한 taxonomy나 duplicate는 멈추는
이유로 삼지 말고 abstain/open QA로 남긴 뒤 나머지를 계속한다.
```
