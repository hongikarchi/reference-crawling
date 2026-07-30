# Parallel source-curation handoff

기준 시각: `2026-07-31 00:21:05 KST`

이 문서는 현재 PC의 Divisare 작업과 다른 PC의 Architizer 작업을 충돌
없이 병렬로 진행하기 위한 기준서다. 각 사이트 내부 데이터를 먼저
정제하고, 모든 source-specific SQLite가 고정된 뒤에 사이트 간 비교와
병합을 시작한다.

## 1. Git 기준점

- Repository: `https://github.com/hongikarchi/reference-crawling.git`
- Shared branch: `main`
- Handoff tag: `handoff-divisare-20260731`
- 이 저장소는 `AGENTS.md`에 따라 feature branch 없이 `main`만 사용한다.
- 태그에는 Divisare curated/metadata/recrawl 코드, 테스트, 문서와 이
  handoff가 포함된다.
- `data/`, HTML snapshot, 실행 로그, 환경 변수와 인증정보는 포함되지
  않는다.

다른 PC의 시작 절차:

```powershell
git clone https://github.com/hongikarchi/reference-crawling.git
cd reference-crawling
git fetch --tags origin
git switch main
git pull --ff-only origin main
git merge-base --is-ancestor handoff-divisare-20260731 HEAD
```

마지막 명령의 exit code가 `0`이어야 한다. 작업 전 `AGENTS.md`,
`CLAUDE.md`, `docs/REFERENCE.md`와
`docs/PROMPT_ARCHITIZER_SOURCE_CURATION.md`를 읽는다.

## 2. 현재 PC 소유 범위

현재 PC만 다음 범위를 수정하거나 실행한다.

- `crawl/divisare/**`
- `canonical/divisare_*`
- Divisare 전용 `tools/`, `tests/`, `docs/`
- `data/enrichment/divisare_metadata_recrawl_v2_4.db`
- `data/enrichment/divisare_html_snapshots_v2_4/`
- Divisare D2 최종 decision과 stable building membership

Divisare immutable metadata v2.1 기준:

- Articles: `29,955`
- Active buildings: `29,891`
- Confirmed / candidate facets: `93,425 / 28,285`
- D2: `66 confirmed / 220 pending / 0 redirects`
- Output SHA-256:
  `8186f49eac8199e0a5cfbd671c952169646b8829840ba9b8b6f85c2244b9deca`
- API/LLM cost: `$0`

실행 중인 HTML recrawl 기준:

- Crawler: `divisare-metadata-recrawl-v2.4.1`
- Parser: `divisare-html-metadata-v2.3`
- Fetch: success `11,635`, pending `18,308`, running `1`, failed `10`,
  not_found `1`
- Parse: `11,167 success / 15 partial / 453 no_content / 0 failed`
- 예상 잔여 시간: 약 `15.3 hours`

두 번째 Divisare crawler를 실행하거나 live DB, WAL, lock, PID를 복사,
삭제 또는 수정하지 않는다.

## 3. 다른 PC 소유 범위

다른 PC는 Architizer source-specific curated SQLite v1만 담당한다.

권장 신규 파일:

- `canonical/architizer_curated.py`
- `tools/build_architizer_curated.py`
- `tests/test_architizer_curated.py`
- `docs/ARCHITIZER_CURATED_DB.md`
- `.claude/ops/jobs/20260731_architizer_curated_v1.md`

수정 금지:

- 모든 Divisare 전용 파일과 runtime artifact
- `core/vocab.py`
- `canonical/schema.py`
- `canonical/assemble_4source.py`
- `canonical/_source_loaders.py`
- `requirements*.txt`
- `data/id_registry*.json`
- Neon/R2 및 production canonical artifact

공통 파일 변경이 필요하면 구현하지 말고 Architizer 문서의 open QA로
남긴다.

## 4. Architizer 입력 전달

Git은 SQLite를 전달하지 않는다. Dropbox 원본을 다른 PC의 로컬
worktree로 복사한 뒤 복사본만 사용한다.

- Source bundle:
  `Dropbox/06_Archibe/make_db_migrate2/data/crawl/architizer.db`
- Expected size: `90,918,912 bytes`
- Expected SHA-256:
  `35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985`
- Local destination: `data/crawl/architizer.db`

Dropbox 안의 SQLite를 직접 쓰기 모드로 열거나 실시간 동기화된 상태로
빌드하지 않는다. 로컬 복사 후 hash를 확인하고 builder는 SQLite
`mode=ro`로 입력을 열어야 한다. `.db-wal`, `.db-shm`, `.env`, 세션,
쿠키와 API key는 전달하거나 commit하지 않는다.

## 5. Architizer 실행 게이트

1. Read-only audit
   - 실제 schema, row count, 관계, coverage, category/tag, 이미지 URL,
     crawl 상태와 내부 중복 근거를 조사한다.
2. Policy와 deterministic builder
   - raw provenance와 normalized claim을 분리한다.
   - 근거가 약하거나 충돌하면 추정하지 않고 candidate/review/NULL로
     남긴다.
3. N=10
   - row-level 검토, integrity/FK, 입력 불변성과 no-clobber를 확인한다.
4. N=100
   - edge case, 누락 accounting, taxonomy와 duplicate 정책을 검증한다.
5. Full
   - 앞선 두 gate가 통과한 뒤에만 immutable artifact를 발행한다.
6. Documentation and Git
   - 입력/output SHA, schema version, row counts, coverage, QA, tests,
     elapsed time과 비용을 기록한다.

Architizer 단계에서는 LLM/Vision, pHash, 이미지 전체 다운로드, 사이트
재크롤링, cross-site matching, embedding/vector, Neon/R2 작업을 하지
않는다. 예상 API/LLM 비용은 `$0`이다.

## 6. Git 동시 작업 규칙

- 각 PC는 자기 사이트 전용 파일만 수정한다.
- `git add .`과 `git add -f data/...`를 사용하지 않는다.
- DB, report artifact, image cache, HTML, 로그와 secret은 commit하지
  않는다.
- 다른 PC는 Architizer 작업을 논리 단위의 atomic commit으로 만든다.
- push 직전에 작업 파일이 깨끗한지 확인하고
  `git pull --rebase origin main`을 실행한다.
- 충돌이 발생하면 공통 파일을 임의로 해결하지 않고 작업을 멈춰
  사용자에게 정확한 충돌 파일을 보고한다.
- force-push와 history rewrite는 금지한다.

## 7. 후속 순서

```text
현재 PC: Divisare recrawl -> reconcile -> D2 -> Divisare freeze
다른 PC: Architizer audit -> N10 -> N100 -> full -> Architizer freeze
이후: Metalocus -> Archello -> E1 pHash -> E2 semantics
최종: cross-site identity -> canonical -> embedding/vector -> Neon/R2
```

Architizer 작업에 사용할 전체 실행 지시는
`docs/PROMPT_ARCHITIZER_SOURCE_CURATION.md`에 있다.
