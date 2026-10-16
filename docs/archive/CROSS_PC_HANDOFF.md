# Cross-PC image-pipeline handoff

이 문서는 Divisare/Architizer 이미지 파이프라인의 현재 상태를 다른 Windows
PC로 안전하게 옮기는 절차다. Git은 코드와 job card만, Dropbox handoff는
Git에서 제외된 immutable DB와 QA 증거만 전달한다.

## 현재 재개 지점

- E1 v1.2: Divisare와 Architizer full 완료
- E2 full v5: 완료
- E3 full v1: 완료
- semantic Vision: v1 N10/N100, v1.1 N10, v1.2 N10까지 보존
- v1.2 N10은 `45/50` 결과의 `complete_with_failures`이며 최종 QA는 `FAIL`
- N100 또는 Full을 이어서 실행하면 안 된다. 다음 유료 단계는 새 immutable
  prompt/runtime 판단과 fresh disjoint N10 승인을 다시 거쳐야 한다.

## 전달 경계

현재 handoff는 다음을 포함한다.

- 최종 E1 v1.2 DB 2개
- E2 full v5와 E3 full v1
- semantic v1/v1.1/v1.2 DB, fixed manifest, compact report, frozen QA ledger
- v1.1/v1.2 review cache와 review sheets
- 정확한 Git `main` bundle
- 파일별 byte size와 SHA-256 manifest

다음은 포함하지 않는다.

- `.env`, `.env.*`, cookie/session, Codex OAuth/API credentials
- `.codex`, `.claude`의 개인 인증 상태
- rejected E1/E2 drafts, smoke DB, WAL/SHM/journal, `.partial`, log
- `.test-tmp`, `.tmp-tests-v11`, `.pytest_cache`, `.venv`
- Neon/R2/vector DB 상태

인증정보는 다른 PC에서 별도로 로그인하거나 발급한다. 특히 기존 Dropbox의
`data/.divisare_session.json`이나 다른 migration bundle의 `secrets/`를 복사하지
않는다.

## 전달 구조

권장 Dropbox 경로는 다음과 같은 새 versioned directory다.

```text
%USERPROFILE%\Dropbox\06_Archibe\reference-crawling-handoff\
  20260813-image-pipeline-<commit>\
    README.md
    HANDOFF_MANIFEST.json
    HANDOFF_READY.json
    reference-crawling-main-<commit>.bundle
    payload\data\...
```

기존 `make_db_migrate2`나 과거 handoff를 덮어쓰지 않는다. 두 curated DB는
이미 아래 Dropbox 경로에 동기화되어 있으므로 새 payload에서 중복하지 않는다.

```text
%USERPROFILE%\Dropbox\06_Archibe\make_db_migrate2\data\curated\
  divisare_metadata_v2_4.db
  architizer_curated_v2_0.db
```

## 원본 PC 절차

1. crawler, builder, Vision runner와 SQLite writer가 없음을 확인한다.
2. `tools/build_current_image_handoff_manifest.py`로 manifest를 생성한다.
3. `tools/copy_cross_pc_handoff.py`로 새 Dropbox payload에 복사한다. 이 도구는
   manifest allowlist 밖의 파일을 읽거나 복사하지 않고, final file을 덮어쓰지
   않으며 `.handoff-partial`에서 재개한다.
4. 모든 destination byte size와 SHA-256을 다시 검증한다.
5. 현재 `main`을 Git bundle로 만들고 `git bundle verify`를 통과시킨다.
6. DB와 bundle이 모두 검증된 뒤에만 `HANDOFF_READY.json`을 마지막으로 둔다.
7. Dropbox Cloud Files `IN_SYNC`와 client `Up to date`를 확인한다.

## 다른 PC 절차

Dropbox 내부 DB를 장기 실행 입력이나 writable output으로 직접 사용하지 않는다.

1. Dropbox 동기화가 끝난 뒤 handoff folder를 `Available offline`으로 만든다.
2. bundle SHA와 `git bundle verify`를 확인한다.
3. 기존 repository가 있으면 bundle을 별도 remote ref로 fetch한 뒤
   `merge --ff-only` 한다. 새 repository라면 bundle에서 clone한 후 GitHub
   `origin`을 별도로 설정한다.
4. 가능하면 repository를 `C:\Users\user\reference-crawling`에 둔다. 기존
   v1.1/v1.2 review-cache provenance는 absolute path까지 고정하므로 다른 Windows
   사용자명이나 경로에서는 과거 terminal validator가 path mismatch를 낼 수 있다.
   새 sample 생성 자체에는 이 cache path 제약이 없다.
5. payload를 repository root로 no-clobber 복사하고 manifest를 다시 검증한다.
6. 두 curated DB도 Dropbox에서 local `data/curated/`로 복사한 뒤 기대 SHA를
   검증한다.
7. 새 virtual environment를 만들고 repository requirements를 설치한다.
   `.venv` 자체를 복사하지 않는다.
8. Codex 로그인과 기타 credential은 새 PC에서 별도로 구성한다.

기존 repo에서 bundle을 받는 예시는 다음과 같다.

```powershell
git status --porcelain=v1 -uall
git fetch "<handoff>\reference-crawling-main-<commit>.bundle" `
  main:refs/remotes/handoff/main
git merge-base --is-ancestor HEAD refs/remotes/handoff/main
git merge --ff-only refs/remotes/handoff/main
git rev-parse HEAD
```

## 동시 실행 금지

두 PC에서 동일 SQLite output path를 동시에 쓰지 않는다. 한 번에 한 PC만 writer
소유권을 가진다. 작업을 넘길 때에는 실행 중 프로세스를 종료하고 immutable
terminal artifact와 job card를 먼저 확정한 다음, 새 versioned output path에서
다른 PC가 재개한다. Dropbox는 전달 매체이지 SQLite 공유 파일시스템이 아니다.
