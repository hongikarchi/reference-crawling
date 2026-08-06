"""Offline contract tests for the trusted Architizer reconciliation manifest."""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crawl.architizer import recrawl_v2
from tools import build_architizer_reconciliation_manifest as manifest
from tools import reconcile_architizer_curated_v2 as reconciliation


class TrustedManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.raw = self.root / "raw.db"
        self.baseline = self.root / "curated_v1_3.db"
        self.sidecar = self.root / "recrawl.db"
        for path, value in (
            (self.raw, "raw"),
            (self.baseline, "baseline"),
        ):
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE fixture(value TEXT NOT NULL)")
            connection.execute("INSERT INTO fixture VALUES (?)", (value,))
            connection.commit()
            connection.close()
        self.raw_sha = manifest.sha256_file(self.raw)
        self.raw_size = self.raw.stat().st_size
        self.baseline_sha = manifest.sha256_file(self.baseline)
        self.baseline_size = self.baseline.stat().st_size
        self.urls = [
            "https://architizer.com/projects/fixture-project/",
            "https://architizer.com/firms/fixture-firm/",
        ]
        self._make_sidecar()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _patch_fixed_identities(self):
        return mock.patch.multiple(
            manifest,
            FIXED_RAW_SHA256=self.raw_sha,
            FIXED_RAW_SIZE_BYTES=self.raw_size,
            FIXED_BASELINE_SHA256=self.baseline_sha,
            FIXED_BASELINE_SIZE_BYTES=self.baseline_size,
        )

    def _make_sidecar(self) -> None:
        connection = recrawl_v2.connect_state(
            self.sidecar,
            source_path=self.raw,
            source_sha256=self.raw_sha,
            source_size=self.raw_size,
        )
        frozen_sha = manifest.url_set_sha256(self.urls)
        connection.execute(
            """
            INSERT INTO runs(
                id,run_kind,started_at,finished_at,status,parser_version,
                arguments_json,source_db_path,source_db_sha256_before,
                source_db_sha256_after,source_db_size,selected_count,
                summary_json,error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                7,
                "full_recrawl_v2",
                "2026-08-01T00:00:00Z",
                "2026-08-02T00:00:00Z",
                "completed",
                recrawl_v2.PARSER_VERSION,
                "{}",
                str(self.raw.resolve()),
                self.raw_sha,
                self.raw_sha,
                self.raw_size,
                len(self.urls),
                json.dumps(
                    {
                        "frozen_target_count": len(self.urls),
                        "frozen_target_urls_sha256": frozen_sha,
                    },
                    sort_keys=True,
                ),
            ),
        )
        # A completed but unused run must not enter the trusted universe.
        connection.execute(
            """
            INSERT INTO runs(
                id,run_kind,started_at,finished_at,status,parser_version,
                arguments_json,source_db_path,source_db_sha256_before,
                source_db_sha256_after,source_db_size,selected_count,
                summary_json,error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                8,
                "network_smoke_n10",
                "2026-07-31T00:00:00Z",
                "2026-07-31T00:10:00Z",
                "completed",
                recrawl_v2.PARSER_VERSION,
                "{}",
                str(self.raw.resolve()),
                self.raw_sha,
                self.raw_sha,
                self.raw_size,
                0,
                "{}",
            ),
        )
        for ordinal, url in enumerate(self.urls, start=1):
            entity_type = "project" if "/projects/" in url else "firm"
            connection.execute(
                """
                INSERT INTO targets(
                    url,entity_type,source_lastmod,priority,primary_reason,
                    status,retryable,attempt_count,created_at,updated_at
                ) VALUES (?,?,?,?,?,'done',0,1,?,?)
                """,
                (
                    url,
                    entity_type,
                    "2026-08-01",
                    ordinal,
                    "fixture",
                    "2026-08-01T00:00:00Z",
                    "2026-08-02T00:00:00Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO run_targets(
                    run_id,url,selection_order,selected_reason,
                    status_before,status_after
                ) VALUES (?,?,?,'fixture','pending','done')
                """,
                (7, url, ordinal),
            )
            connection.execute(
                """
                INSERT INTO http_attempts(
                    run_id,target_url,request_kind,requested_url,attempt_number,
                    started_at,finished_at,duration_ms,outcome,http_status,
                    final_url,content_type,response_bytes,sha256,gzip_path,
                    retryable,block_signals_json,error
                ) VALUES (?,?,?,?,1,?,?,1,'success',200,?,'text/html',100,?,?,0,'[]',NULL)
                """,
                (
                    7,
                    url,
                    f"{entity_type}_page",
                    url,
                    "2026-08-01T00:00:00Z",
                    "2026-08-01T00:00:01Z",
                    url,
                    f"{ordinal:064X}",
                    f"{ordinal}.html.gz",
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO metadata_versions(
                    run_id,target_url,entity_type,snapshot_sha256,
                    parser_version,metadata_version,parsed_at,parse_status,
                    quality,identity_status,identity_json,raw_embedded_json,
                    dom_json,resolved_json,conflict_json
                ) VALUES (?,?,?,?,?,?,?,'complete','high','valid',?,?,?,?,?)
                """,
                (
                    7,
                    url,
                    entity_type,
                    f"{ordinal:064X}",
                    recrawl_v2.PARSER_VERSION,
                    recrawl_v2.METADATA_VERSION,
                    "2026-08-02T00:00:00Z",
                    "{}",
                    "{}",
                    "{}",
                    "{}",
                    "{}",
                ),
            )
            version_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE targets SET current_metadata_version_id=?,"
                "last_good_version_id=? WHERE url=?",
                (version_id, version_id, url),
            )
            connection.execute(
                "INSERT INTO run_metadata_versions(run_id,version_id,target_url) "
                "VALUES (?,?,?)",
                (7, version_id, url),
            )
        connection.commit()
        connection.close()

    def _build(self, output: Path | None = None):
        destination = output or self.root / "trusted.json"
        with self._patch_fixed_identities():
            return manifest.build_trusted_manifest(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=destination,
            )

    def test_manifest_is_deterministic_and_matches_reconciliation_contract(self) -> None:
        first_path = self.root / "one" / "trusted.json"
        second_path = self.root / "two" / "trusted.json"
        first = self._build(first_path)
        second = self._build(second_path)
        self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
        self.assertEqual(first["sha256"], second["sha256"])
        payload = first["payload"]
        self.assertEqual(
            [item["id"] for item in payload["sidecar_contract"]["required_completed_runs"]],
            [7],
        )
        required_run = payload["sidecar_contract"]["required_completed_runs"][0]
        self.assertEqual(
            required_run["referenced_parser_versions"],
            [recrawl_v2.PARSER_VERSION],
        )
        self.assertEqual(
            required_run["referenced_metadata_versions"],
            [recrawl_v2.METADATA_VERSION],
        )
        self.assertEqual(payload["sidecar_contract"]["pending_target_count"], 0)
        self.assertEqual(payload["sidecar_contract"]["active_run_count"], 0)
        self.assertNotIn(str(self.root), first_path.read_text(encoding="utf-8"))

        connection = sqlite3.connect(self.sidecar)
        connection.row_factory = sqlite3.Row
        input_before = {
            role: {**value, "path_label": role}
            for role, value in payload["inputs"].items()
        }
        state_meta = dict(connection.execute("SELECT key,value FROM state_meta"))
        with mock.patch.multiple(
            reconciliation,
            FIXED_RAW_SHA256=self.raw_sha,
            FIXED_RAW_SIZE_BYTES=self.raw_size,
            FIXED_BASELINE_SHA256=self.baseline_sha,
            FIXED_BASELINE_SIZE_BYTES=self.baseline_size,
        ):
            validated = reconciliation._validate_trusted_manifest(
                manifest_path=first_path,
                input_before=input_before,
                sidecar=connection,
                sidecar_meta=state_meta,
            )
        connection.close()
        self.assertEqual(validated["sha256"], first["sha256"])

    def test_lock_no_overwrite_and_exception_cleanup(self) -> None:
        output = self.root / "trusted.json"
        with recrawl_v2.SidecarLock(self.sidecar):
            with self.assertRaisesRegex(manifest.ManifestError, "lock already exists"):
                self._build(output)
        self.assertFalse(output.exists())
        self.assertFalse(Path(str(self.sidecar) + ".lock").exists())

        self._build(output)
        original = output.read_bytes()
        with self.assertRaisesRegex(manifest.ManifestError, "already exists"):
            self._build(output)
        self.assertEqual(output.read_bytes(), original)
        self.assertFalse(Path(str(self.sidecar) + ".lock").exists())

    def test_raw_and_baseline_are_rehashed_immediately_before_publish(self) -> None:
        real_dump = manifest.json.dump
        for index, (role, target) in enumerate(
            (("legacy_raw", self.raw), ("curated_v1_3", self.baseline))
        ):
            with self.subTest(role=role):
                original = target.read_bytes()

                def dump_then_mutate(*args, **kwargs):
                    result = real_dump(*args, **kwargs)
                    with target.open("ab") as handle:
                        handle.write(b"drift")
                    return result

                output = self.root / f"drift-{index}.json"
                try:
                    with self._patch_fixed_identities(), mock.patch.object(
                        manifest.json,
                        "dump",
                        side_effect=dump_then_mutate,
                    ), self.assertRaisesRegex(
                        manifest.ManifestError,
                        f"{role} changed during manifest freeze",
                    ):
                        manifest.build_trusted_manifest(
                            raw_path=self.raw,
                            baseline_path=self.baseline,
                            sidecar_path=self.sidecar,
                            output_path=output,
                        )
                finally:
                    target.write_bytes(original)
                self.assertFalse(output.exists())
                self.assertFalse(Path(str(self.sidecar) + ".lock").exists())

    def test_input_drift_inside_manifest_link_is_rolled_back(self) -> None:
        output = self.root / "post-link-drift.json"
        original_raw = self.raw.read_bytes()
        real_link = manifest.os.link

        def link_then_mutate(source: Path, destination: Path) -> None:
            real_link(source, destination)
            with self.raw.open("ab") as handle:
                handle.write(b"drift")

        try:
            with self._patch_fixed_identities(), mock.patch.object(
                manifest.os,
                "link",
                side_effect=link_then_mutate,
            ), self.assertRaisesRegex(
                manifest.ManifestError,
                "legacy_raw changed during manifest freeze",
            ):
                manifest.build_trusted_manifest(
                    raw_path=self.raw,
                    baseline_path=self.baseline,
                    sidecar_path=self.sidecar,
                    output_path=output,
                )
        finally:
            self.raw.write_bytes(original_raw)
        self.assertFalse(output.exists())
        self.assertFalse(Path(str(self.sidecar) + ".lock").exists())

    def test_pending_active_foreign_key_and_frozen_hash_are_hard_gates(self) -> None:
        cases = []
        pending = self.root / "pending.db"
        shutil.copyfile(self.sidecar, pending)
        connection = sqlite3.connect(pending)
        connection.execute("UPDATE targets SET status='pending' WHERE url=?", (self.urls[0],))
        connection.commit()
        connection.close()
        cases.append((pending, "not converged"))

        missing_last_good = self.root / "missing_last_good.db"
        shutil.copyfile(self.sidecar, missing_last_good)
        connection = sqlite3.connect(missing_last_good)
        connection.execute(
            "UPDATE targets SET last_good_version_id=NULL WHERE url=?",
            (self.urls[0],),
        )
        connection.commit()
        connection.close()
        cases.append((missing_last_good, "done targets without last-good"))

        active = self.root / "active.db"
        shutil.copyfile(self.sidecar, active)
        connection = sqlite3.connect(active)
        connection.execute("UPDATE runs SET status='running',finished_at=NULL WHERE id=7")
        connection.commit()
        connection.close()
        cases.append((active, "active/incomplete"))

        wrong_hash = self.root / "wrong_hash.db"
        shutil.copyfile(self.sidecar, wrong_hash)
        connection = sqlite3.connect(wrong_hash)
        connection.execute(
            "UPDATE runs SET summary_json=? WHERE id=7",
            (json.dumps({"frozen_target_urls_sha256": "A" * 64}),),
        )
        connection.commit()
        connection.close()
        cases.append((wrong_hash, "frozen target SHA mismatch"))

        orphan = self.root / "orphan.db"
        shutil.copyfile(self.sidecar, orphan)
        connection = sqlite3.connect(orphan)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO target_reasons(
                url,reason,discovery_source,priority,source_lastmod,
                first_seen_at,last_seen_at,input_lineage_json
            ) VALUES ('https://architizer.com/projects/orphan/','fixture',
                      'fixture',1,NULL,'x','x','{}')
            """
        )
        connection.commit()
        connection.close()
        cases.append((orphan, "foreign_key_check failed"))

        for index, (sidecar, message) in enumerate(cases):
            with self.subTest(message=message):
                output = self.root / f"rejected-{index}.json"
                with self._patch_fixed_identities(), self.assertRaisesRegex(
                    manifest.ManifestError, message
                ):
                    manifest.build_trusted_manifest(
                        raw_path=self.raw,
                        baseline_path=self.baseline,
                        sidecar_path=sidecar,
                        output_path=output,
                    )
                self.assertFalse(output.exists())
                self.assertFalse(Path(str(sidecar) + ".lock").exists())

    def test_reconciliation_recomputes_the_exact_manifest_contract(self) -> None:
        built = self._build(self.root / "trusted-exact.json")
        original = built["payload"]
        input_before = {
            role: {**value, "path_label": role}
            for role, value in original["inputs"].items()
        }
        connection = sqlite3.connect(self.sidecar)
        connection.row_factory = sqlite3.Row
        state_meta = dict(connection.execute("SELECT key,value FROM state_meta"))

        cases: list[tuple[str, dict[str, object]]] = []

        def mutate(label: str, path: tuple[str, ...], value: object) -> None:
            candidate = copy.deepcopy(original)
            cursor: dict[str, object] = candidate
            for key in path[:-1]:
                cursor = cursor[key]  # type: ignore[assignment,index]
            cursor[path[-1]] = value
            cases.append((label, candidate))

        missing_top = copy.deepcopy(original)
        del missing_top["artifact_kind"]
        cases.append(("missing top-level key", missing_top))
        extra_top = copy.deepcopy(original)
        extra_top["unexpected"] = True
        cases.append(("extra top-level key", extra_top))
        missing_input_field = copy.deepcopy(original)
        del missing_input_field["inputs"]["legacy_raw"]["size_bytes"]
        cases.append(("missing input field", missing_input_field))
        extra_input_field = copy.deepcopy(original)
        extra_input_field["inputs"]["curated_v1_3"]["path"] = "forbidden"
        cases.append(("extra input field", extra_input_field))
        missing_contract = copy.deepcopy(original)
        del missing_contract["sidecar_contract"]["input_integrity"]
        cases.append(("missing sidecar contract field", missing_contract))
        extra_contract = copy.deepcopy(original)
        extra_contract["sidecar_contract"]["claim"] = "unverified"
        cases.append(("extra sidecar contract field", extra_contract))

        contract = original["sidecar_contract"]
        mutations = {
            "schema version": "wrong-schema",
            "source SHA": "A" * 64,
            "source size": int(contract["source_db_size"]) + 1,
            "pending count": 1,
            "active run count": 1,
            "done mismatch count": 1,
            "invalid last-good count": 1,
            "last-good count": int(contract["last_good_target_count"]) + 1,
            "last-good URL hash": "B" * 64,
            "evidence counts": {},
            "parser versions": [],
            "metadata versions": [],
            "required runs": [],
            "input integrity": {
                "quick_check": "broken",
                "foreign_key_violation_count": 0,
            },
        }
        fields = {
            "schema version": "schema_version",
            "source SHA": "source_db_sha256",
            "source size": "source_db_size",
            "pending count": "pending_target_count",
            "active run count": "active_run_count",
            "done mismatch count": "done_without_last_good_count",
            "invalid last-good count": "invalid_last_good_link_count",
            "last-good count": "last_good_target_count",
            "last-good URL hash": "last_good_target_urls_sha256",
            "evidence counts": "last_good_evidence_kind_counts",
            "parser versions": "parser_versions",
            "metadata versions": "metadata_versions",
            "required runs": "required_completed_runs",
            "input integrity": "input_integrity",
        }
        for label, value in mutations.items():
            mutate(label, ("sidecar_contract", fields[label]), value)

        try:
            for index, (label, candidate) in enumerate(cases):
                with self.subTest(label=label):
                    path = self.root / f"tampered-{index}.json"
                    path.write_text(
                        json.dumps(candidate, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    with mock.patch.multiple(
                        reconciliation,
                        FIXED_RAW_SHA256=self.raw_sha,
                        FIXED_RAW_SIZE_BYTES=self.raw_size,
                        FIXED_BASELINE_SHA256=self.baseline_sha,
                        FIXED_BASELINE_SIZE_BYTES=self.baseline_size,
                    ), self.assertRaises(reconciliation.ReconciliationError):
                        reconciliation._validate_trusted_manifest(
                            manifest_path=path,
                            input_before=input_before,
                            sidecar=connection,
                            sidecar_meta=state_meta,
                        )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
