from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.copy_cross_pc_handoff import load_manifest as load_copy_manifest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "build_current_image_handoff_manifest.py"
)
SPEC = importlib.util.spec_from_file_location("build_current_image_handoff_manifest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
manifest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manifest
SPEC.loader.exec_module(manifest)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class CurrentImageHandoffManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.primary = self._write("artifacts/primary.db", b"primary")
        self.support = self._write("reports/report.json", b'{"ok":true}\n')
        self.lock = self._write("artifacts/run.db.lock", b"1")
        self._write("review/cache/image-002.jpg", b"image-two")
        self._write("review/cache/image-001.jpg", b"image-one")
        self._write("review/sheets/manifest.json", b"{}\n")
        self.output = self.root / "handoff.json"

        self.primary_specs = (
            manifest.FileSpec(
                "artifacts/primary.db",
                "primary",
                "primary_database",
                self.primary.stat().st_size,
                _sha(self.primary.read_bytes()),
            ),
        )
        self.lock_specs = (
            manifest.FileSpec(
                "artifacts/run.db.lock",
                "persistent",
                "persistent_lock",
                allow_persistent_lock=True,
            ),
        )
        self.support_specs = (
            manifest.FileSpec("reports/report.json", "report", "report_or_manifest"),
        )
        self.directory_specs = (
            manifest.DirectorySpec("review/cache", "cache", "review_cache"),
            manifest.DirectorySpec("review/sheets", "sheets", "review_sheet"),
        )
        self.external_specs = (
            manifest.ExternalRequirement(
                "data/curated/source.db",
                "source",
                123,
                "a" * 64,
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, relative: str, payload: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def _patch_specs(self):
        return mock.patch.multiple(
            manifest,
            PRIMARY_FILES=self.primary_specs,
            PERSISTENT_LOCKS=self.lock_specs,
            SUPPORTING_FILES=self.support_specs,
            RECURSIVE_DIRECTORIES=self.directory_specs,
            EXTERNAL_REQUIREMENTS=self.external_specs,
        )

    def _build(self, output: Path | None = None):
        git_state = {"head": "1" * 40, "origin_main": "2" * 40}
        with self._patch_specs(), mock.patch.object(
            manifest, "_read_git_state", return_value=git_state
        ):
            return manifest.build_current_image_handoff_manifest(
                repo_root=self.root,
                output_path=output or self.output,
                now=lambda: datetime(2026, 8, 13, 1, 2, 3, tzinfo=timezone.utc),
            )

    def test_allowlist_manifest_hashes_every_directory_file(self) -> None:
        payload = self._build()
        on_disk = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(payload, on_disk)
        self.assertEqual(payload["generated_at_utc"], "2026-08-13T01:02:03Z")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["repository"]["head"], "1" * 40)
        self.assertEqual(payload["repository"]["origin_main"], "2" * 40)

        artifacts = payload["artifacts"]
        paths = [entry["path"] for entry in artifacts]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(
            paths,
            [
                "artifacts/primary.db",
                "artifacts/run.db.lock",
                "reports/report.json",
                "review/cache/image-001.jpg",
                "review/cache/image-002.jpg",
                "review/sheets/manifest.json",
            ],
        )
        self.assertEqual(payload["totals"]["file_count"], 6)
        self.assertEqual(
            payload["totals"]["size_bytes"],
            sum((self.root / path).stat().st_size for path in paths),
        )
        primary = next(item for item in artifacts if item["path"] == "artifacts/primary.db")
        self.assertTrue(primary["pinned_identity_validated"])
        self.assertEqual(primary["sha256"], _sha(b"primary"))
        self.assertFalse(
            next(item for item in artifacts if item["path"] == "reports/report.json")[
                "pinned_identity_validated"
            ]
        )
        self.assertEqual(payload["external_requirements"][0]["path"], "data/curated/source.db")
        self.assertNotIn("data/curated/source.db", paths)
        self.assertNotIn(str(self.root), self.output.read_text(encoding="utf-8"))

        copy_entries = load_copy_manifest(self.output)
        self.assertEqual([entry.path for entry in copy_entries], paths)
        self.assertTrue(
            next(
                entry
                for entry in copy_entries
                if entry.path == "artifacts/primary.db"
            ).sqlite_immutable
        )
        self.assertTrue(
            all(
                not entry.sqlite_immutable
                for entry in copy_entries
                if entry.path != "artifacts/primary.db"
            )
        )
        self.assertEqual(
            [(entry.bytes, entry.sha256) for entry in copy_entries],
            [
                (entry["size_bytes"], entry["sha256"])
                for entry in artifacts
            ],
        )

    def test_pinned_identity_mismatch_fails_before_publication(self) -> None:
        bad = (
            manifest.FileSpec(
                "artifacts/primary.db",
                "primary",
                "primary_database",
                self.primary.stat().st_size,
                "f" * 64,
            ),
        )
        with (
            self._patch_specs(),
            mock.patch.object(manifest, "PRIMARY_FILES", bad),
            mock.patch.object(
                manifest,
                "_read_git_state",
                return_value={"head": "1" * 40, "origin_main": "2" * 40},
            ),
            self.assertRaisesRegex(
                manifest.HandoffManifestError, "pinned identity mismatch"
            ),
        ):
            manifest.build_current_image_handoff_manifest(
                repo_root=self.root,
                output_path=self.output,
            )
        self.assertFalse(self.output.exists())

    def test_no_clobber_preserves_existing_manifest(self) -> None:
        self.output.write_bytes(b"keep-me")
        with self.assertRaisesRegex(manifest.HandoffManifestError, "already exists"):
            self._build()
        self.assertEqual(self.output.read_bytes(), b"keep-me")

    def test_recursive_log_or_sqlite_transient_is_fail_closed(self) -> None:
        for index, name in enumerate(("run.log", "state.db-wal", ".env")):
            with self.subTest(name=name):
                forbidden = self._write(f"review/cache/{name}", b"forbidden")
                destination = self.root / f"rejected-{index}.json"
                try:
                    with self.assertRaisesRegex(
                        manifest.HandoffManifestError, "forbidden artifact"
                    ):
                        self._build(destination)
                    self.assertFalse(destination.exists())
                finally:
                    forbidden.unlink()

    def test_git_revisions_are_read_with_read_only_commands(self) -> None:
        responses = [
            mock.Mock(returncode=0, stdout="A" * 40 + "\n", stderr=""),
            mock.Mock(returncode=0, stdout="B" * 40 + "\n", stderr=""),
        ]
        with mock.patch.object(manifest.subprocess, "run", side_effect=responses) as run:
            state = manifest._read_git_state(self.root)
        self.assertEqual(state, {"head": "a" * 40, "origin_main": "b" * 40})
        self.assertEqual(run.call_args_list[0].args[0], ["git", "rev-parse", "HEAD"])
        self.assertEqual(run.call_args_list[1].args[0], ["git", "rev-parse", "origin/main"])
        self.assertTrue(all(call.kwargs["check"] is False for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
