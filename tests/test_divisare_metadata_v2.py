import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from requests.exceptions import TooManyRedirects

from crawl.divisare.metadata_v2 import (
    looks_like_login_wall,
    parse_project_metadata,
)
from tools.recrawl_divisare_metadata_v2 import (
    AuthenticationExpiredError,
    STATE_SCHEMA_SQL,
    circuit_breaker_status,
    exclusive_state_lock,
    fetch_job,
    parse_status_for,
    reparse_current_snapshots,
    save_metadata_version,
    seed_jobs,
    update_failure_streaks,
)


def project_html(
    *,
    area="1,500 sqm",
    description="A careful project description.",
    extra_head="",
):
    return f"""
    <html>
      <head><title>Sample Project - Divisare</title>{extra_head}</head>
      <body>
        <h1>divisare</h1>
        <h1>Sample Project</h1>
        <div class="project" data-project-id="123">
          <div class="header"><div class="abstract">Short abstract.</div></div>
          <div class="sidebar">
            <div class="content">
              <div class="section">Location</div><div>Spain - Madrid</div>
            </div>
            <div class="content">
              <div class="section">Project Year</div><div>Completed 2022</div>
            </div>
          </div>
          <div class="project_fact">
            <ul><li><span>Client</span>Sample Client</li></ul>
          </div>
          <div class="project_fact">
            <ul><li><span>Built Surface</span>{area}</li></ul>
          </div>
          <div class="description">
            <div class="image">
              <img src="photo.jpg">
              <div class="caption"><p>Exterior photograph</p></div>
              <p>Photographer Name</p>
            </div>
            <p>{description}</p>
            <p>Add to collection Choose collection... New collection...</p>
          </div>
        </div>
      </body>
    </html>
    """


class DivisareHtmlMetadataTests(unittest.TestCase):
    def test_description_excludes_media_caption_and_collection_ui(self):
        parsed = parse_project_metadata(
            project_html(),
            "https://divisare.com/projects/123-sample-project",
        )

        self.assertEqual(parsed.article_id, 123)
        self.assertEqual(parsed.name, "Sample Project")
        self.assertEqual(parsed.description_prose, "A careful project description.")
        self.assertNotIn("Exterior photograph", parsed.description_prose)
        self.assertNotIn("Photographer", parsed.description_prose)
        self.assertNotIn("Add to collection", parsed.description_prose)
        self.assertEqual(parsed.description_quality, "dom_prose_paragraphs")

    def test_structured_sidebar_fields_and_decimal_area(self):
        parsed = parse_project_metadata(
            project_html(area="1.250,5 m2"),
            "https://divisare.com/projects/123-sample-project",
        )

        self.assertEqual(parsed.location_country, "Spain")
        self.assertEqual(parsed.location_city, "Madrid")
        self.assertEqual(parsed.project_year, 2022)
        self.assertEqual(parsed.area_sqm, 1250.5)
        self.assertEqual(parsed.area_raw, "1.250,5 m2")
        self.assertEqual(
            parsed.details["area_unit_status"],
            "implicit_square_metres_divisare",
        )
        self.assertEqual(parsed.details["area_confidence"], 0.75)

    def test_dot_thousands_area_and_project_identity(self):
        parsed = parse_project_metadata(
            project_html(area="1.500"),
            "https://divisare.com/projects/123-sample-project",
        )

        self.assertEqual(parsed.area_sqm, 1500.0)
        self.assertTrue(parsed.details["project_dom_id_matches_url"])

    def test_wrong_project_dom_identity_is_exposed(self):
        parsed = parse_project_metadata(
            project_html().replace('data-project-id="123"', 'data-project-id="999"'),
            "https://divisare.com/projects/123-sample-project",
        )

        self.assertFalse(parsed.details["project_dom_id_matches_url"])
        self.assertEqual(parsed.details["project_dom_id"], 999)
        self.assertEqual(parse_status_for(parsed), "failed")

    def test_final_url_and_project_dom_cardinality_are_required(self):
        wrong_url = parse_project_metadata(
            project_html(),
            "https://divisare.com/projects/999-other-project",
            expected_article_id=123,
        )
        duplicate_dom = parse_project_metadata(
            project_html().replace(
                "</body>",
                '<div class="project" data-project-id="123"></div></body>',
            ),
            "https://divisare.com/projects/123-sample-project",
        )

        self.assertFalse(wrong_url.details["page_url_matches_expected"])
        self.assertEqual(parse_status_for(wrong_url), "failed")
        self.assertEqual(duplicate_dom.details["project_dom_count"], 2)
        self.assertEqual(parse_status_for(duplicate_dom), "failed")

    def test_nested_paragraphs_are_fallback_only(self):
        html = project_html().replace(
            "<p>A careful project description.</p>",
            "<div><p>Nested fallback prose.</p></div>",
        ).replace(
            "<p>Add to collection Choose collection... New collection...</p>",
            "",
        )
        parsed = parse_project_metadata(
            html,
            "https://divisare.com/projects/123-sample-project",
        )

        self.assertEqual(parsed.description_prose, "Nested fallback prose.")
        self.assertEqual(parsed.description_quality, "dom_text_fallback_review")
        self.assertTrue(parsed.details["fallback_used"])

    def test_title_or_content_does_not_confirm_article_kind(self):
        parsed = parse_project_metadata(
            project_html(description="Plans and sections explain the building."),
            "https://divisare.com/projects/123-plans-and-sections",
        )

        self.assertIsNone(parsed.explicit_article_kind)

    def test_explicit_dom_marker_can_supply_article_kind(self):
        parsed = parse_project_metadata(
            project_html(
                extra_head=(
                    '<meta name="divisare:article_kind" '
                    'content="Drawing Feature">'
                )
            ),
            "https://divisare.com/projects/123-sample-project",
        )

        self.assertEqual(parsed.explicit_article_kind, "drawing_feature")
        self.assertEqual(
            parsed.details["explicit_article_kind_source"],
            "meta:divisare:article_kind",
        )

    def test_login_wall_detection(self):
        html = """
        <html><head><title>Log in - Divisare</title></head>
        <body><form action="/people/login">
          <input name="person[email]">
        </form></body></html>
        """
        self.assertTrue(
            looks_like_login_wall(
                html,
                "https://divisare.com/login",
                200,
            )
        )
        self.assertFalse(
            looks_like_login_wall(
                project_html(),
                "https://divisare.com/projects/123-sample-project",
                200,
            )
        )
        self.assertFalse(
            looks_like_login_wall(
                project_html().replace(
                    "Sample Project - Divisare",
                    "Login House - Divisare",
                ),
                "https://divisare.com/projects/123-login-house",
                200,
            )
        )

    def test_failed_identity_parse_is_not_current_metadata(self):
        state = sqlite3.connect(":memory:")
        state.row_factory = sqlite3.Row
        state.execute("PRAGMA foreign_keys=ON")
        state.executescript(STATE_SCHEMA_SQL)
        state.execute(
            """
            INSERT INTO recrawl_runs(
                started_at,status,max_items,delay_seconds,refresh_mode
            ) VALUES ('2026-07-28T00:00:00+00:00','running',2,3.0,0)
            """
        )
        state.execute(
            """
            INSERT INTO article_html_jobs(
                article_id,source_url,priority,reasons_json,
                queued_at,updated_at
            ) VALUES (
                123,'https://divisare.com/projects/123-sample-project',
                1,'[]','2026-07-28T00:00:00+00:00',
                '2026-07-28T00:00:00+00:00'
            )
            """
        )
        for snapshot_id, digest, is_current in (
            (1, "a" * 64, 0),
            (2, "b" * 64, 1),
        ):
            state.execute(
                """
                INSERT INTO article_html_snapshots(
                    snapshot_id,article_id,html_sha256,byte_size,snapshot_path,
                    http_status,final_url,response_headers_json,fetched_at,
                    run_id,is_current
                ) VALUES (?,?,?,?,?,200,?,'{}',?,1,?)
                """,
                (
                    snapshot_id,
                    123,
                    digest,
                    100,
                    "unused-%s.html.gz" % snapshot_id,
                    "https://divisare.com/projects/123-sample-project",
                    "2026-07-28T00:00:00+00:00",
                    is_current,
                ),
            )
        job = state.execute(
            "SELECT * FROM article_html_jobs WHERE article_id=123"
        ).fetchone()
        valid_status, _ = save_metadata_version(
            state,
            job=job,
            snapshot_id=1,
            content=project_html().encode("utf-8"),
        )
        invalid_status, _ = save_metadata_version(
            state,
            job=job,
            snapshot_id=2,
            content=project_html().replace(
                'data-project-id="123"',
                'data-project-id="999"',
            ).encode("utf-8"),
        )

        self.assertEqual(valid_status, "success")
        self.assertEqual(invalid_status, "failed")
        current = state.execute(
            """
            SELECT snapshot_id
            FROM article_metadata_versions
            WHERE article_id=123 AND is_current=1
            """
        ).fetchall()
        self.assertEqual([row["snapshot_id"] for row in current], [1])
        failed_current = state.execute(
            """
            SELECT is_current
            FROM article_metadata_versions
            WHERE article_id=123 AND snapshot_id=2
            """
        ).fetchone()[0]
        self.assertEqual(failed_current, 0)
        state.close()

    def test_reparse_skips_snapshot_already_parsed_by_current_version(self):
        state = sqlite3.connect(":memory:")
        state.row_factory = sqlite3.Row
        state.execute("PRAGMA foreign_keys=ON")
        state.executescript(STATE_SCHEMA_SQL)
        state.execute(
            """
            INSERT INTO recrawl_runs(
                started_at,status,max_items,delay_seconds,refresh_mode
            ) VALUES ('2026-07-28T00:00:00+00:00','running',1,3.0,0)
            """
        )
        state.execute(
            """
            INSERT INTO article_html_jobs(
                article_id,source_url,priority,reasons_json,
                queued_at,updated_at
            ) VALUES (
                123,'https://divisare.com/projects/123-sample-project',
                1,'[]','2026-07-28T00:00:00+00:00',
                '2026-07-28T00:00:00+00:00'
            )
            """
        )
        state.execute(
            """
            INSERT INTO article_html_snapshots(
                snapshot_id,article_id,html_sha256,byte_size,snapshot_path,
                http_status,final_url,response_headers_json,fetched_at,
                run_id,is_current
            ) VALUES (
                1,123,?,100,'unused.html.gz',200,
                'https://divisare.com/projects/123-sample-project',
                '{}','2026-07-28T00:00:00+00:00',1,1
            )
            """,
            ("a" * 64,),
        )
        job = state.execute(
            "SELECT * FROM article_html_jobs WHERE article_id=123"
        ).fetchone()
        save_metadata_version(
            state,
            job=job,
            snapshot_id=1,
            content=project_html().encode("utf-8"),
        )

        self.assertEqual(
            reparse_current_snapshots(state, max_items=1),
            0,
        )
        self.assertEqual(
            state.execute(
                "SELECT COUNT(*) FROM article_metadata_versions"
            ).fetchone()[0],
            1,
        )
        state.close()

    def test_reparse_rejects_snapshot_integrity_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.html.gz"
            content = project_html().encode("utf-8")
            with gzip.open(snapshot_path, "wb") as handle:
                handle.write(content)

            state = sqlite3.connect(":memory:")
            state.row_factory = sqlite3.Row
            state.execute("PRAGMA foreign_keys=ON")
            state.executescript(STATE_SCHEMA_SQL)
            state.execute(
                """
                INSERT INTO recrawl_runs(
                    started_at,status,max_items,delay_seconds,refresh_mode
                ) VALUES ('2026-07-28T00:00:00+00:00','running',1,3.0,0)
                """
            )
            state.execute(
                """
                INSERT INTO article_html_jobs(
                    article_id,source_url,priority,reasons_json,
                    queued_at,updated_at
                ) VALUES (
                    123,'https://divisare.com/projects/123-sample-project',
                    1,'[]','2026-07-28T00:00:00+00:00',
                    '2026-07-28T00:00:00+00:00'
                )
                """
            )
            state.execute(
                """
                INSERT INTO article_html_snapshots(
                    article_id,html_sha256,byte_size,snapshot_path,
                    http_status,final_url,response_headers_json,fetched_at,
                    run_id,is_current
                ) VALUES (
                    123,?,? ,?,200,
                    'https://divisare.com/projects/123-sample-project',
                    '{}','2026-07-28T00:00:00+00:00',1,1
                )
                """,
                ("0" * 64, len(content), str(snapshot_path)),
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "snapshot integrity mismatch",
            ):
                reparse_current_snapshots(state, max_items=1)
            self.assertEqual(
                state.execute(
                    "SELECT COUNT(*) FROM article_metadata_versions"
                ).fetchone()[0],
                0,
            )
            state.close()

    def test_existing_state_seed_scope_cannot_expand_silently(self):
        parent = sqlite3.connect(":memory:")
        parent.row_factory = sqlite3.Row
        parent.execute(
            """
            CREATE TABLE article_recrawl_queue_v2(
                article_id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL,
                priority INTEGER NOT NULL,
                reasons_json TEXT NOT NULL
            )
            """
        )
        parent.executemany(
            """
            INSERT INTO article_recrawl_queue_v2
            VALUES (?,?,?,?)
            """,
            (
                (
                    1,
                    "https://divisare.com/projects/1-one",
                    1,
                    "[]",
                ),
                (
                    2,
                    "https://divisare.com/projects/2-two",
                    1,
                    "[]",
                ),
            ),
        )
        state = sqlite3.connect(":memory:")
        state.row_factory = sqlite3.Row
        state.executescript(STATE_SCHEMA_SQL)

        self.assertEqual(seed_jobs(parent, state, seed_limit=1), 1)
        with self.assertRaisesRegex(RuntimeError, "different seed scope"):
            seed_jobs(parent, state, seed_limit=None)
        self.assertEqual(
            state.execute("SELECT COUNT(*) FROM article_html_jobs").fetchone()[0],
            1,
        )
        state.close()
        parent.close()

    def test_fetch_circuit_breaker_stops_repeated_blocking(self):
        blocked = failures = 0
        for _ in range(2):
            blocked, failures = update_failure_streaks(
                "blocked",
                consecutive_blocked=blocked,
                consecutive_failures=failures,
                max_consecutive_blocked=3,
                max_consecutive_failures=10,
            )
        with self.assertRaisesRegex(
            RuntimeError,
            "3 consecutive blocked responses",
        ):
            update_failure_streaks(
                "blocked",
                consecutive_blocked=blocked,
                consecutive_failures=failures,
                max_consecutive_blocked=3,
                max_consecutive_failures=10,
            )

        self.assertEqual(
            update_failure_streaks(
                "success",
                consecutive_blocked=2,
                consecutive_failures=2,
                max_consecutive_blocked=3,
                max_consecutive_failures=10,
            ),
            (0, 0),
        )
        self.assertEqual(
            circuit_breaker_status("success", "failed"),
            "failed",
        )
        self.assertEqual(
            circuit_breaker_status("success", "partial"),
            "success",
        )

    def test_state_lock_blocks_a_second_crawler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.db"
            with exclusive_state_lock(state_path):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "already locked by another process",
                ):
                    with exclusive_state_lock(state_path):
                        pass

    def test_login_wall_aborts_before_terminal_status_commit(self):
        class FakeResponse:
            status_code = 200
            url = "https://divisare.com/people/login"
            text = (
                "<html><title>Login - Divisare</title>"
                "<form action='/people/login'>"
                "<input name='person[email]'></form></html>"
            )
            content = text.encode("utf-8")
            headers = {}

        class FakeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        class NoWait:
            def wait(self):
                return None

        class RedirectLoopSession:
            def get(self, *_args, **_kwargs):
                raise TooManyRedirects("redirect loop")

        with tempfile.TemporaryDirectory() as temp_dir:
            state = sqlite3.connect(":memory:")
            state.row_factory = sqlite3.Row
            state.execute("PRAGMA foreign_keys=ON")
            state.executescript(STATE_SCHEMA_SQL)
            run_id = int(
                state.execute(
                    """
                    INSERT INTO recrawl_runs(
                        started_at,status,max_items,delay_seconds,refresh_mode
                    ) VALUES ('2026-07-28T00:00:00+00:00','running',1,3.0,0)
                    """
                ).lastrowid
            )
            state.execute(
                """
                INSERT INTO article_html_jobs(
                    article_id,source_url,priority,reasons_json,
                    queued_at,updated_at
                ) VALUES (
                    123,'https://divisare.com/projects/123-sample-project',
                    1,'[]','2026-07-28T00:00:00+00:00',
                    '2026-07-28T00:00:00+00:00'
                )
                """
            )
            state.commit()
            job = state.execute(
                "SELECT * FROM article_html_jobs WHERE article_id=123"
            ).fetchone()

            with self.assertRaises(AuthenticationExpiredError):
                fetch_job(
                    state,
                    run_id=run_id,
                    job=job,
                    session=FakeSession(),
                    rate_limiter=NoWait(),
                    snapshot_root=Path(temp_dir),
                    refresh=False,
                )
            self.assertEqual(
                state.execute(
                    """
                    SELECT fetch_status
                    FROM article_html_jobs
                    WHERE article_id=123
                    """
                ).fetchone()[0],
                "running",
            )
            self.assertEqual(
                state.execute(
                    "SELECT COUNT(*) FROM article_html_snapshots"
                ).fetchone()[0],
                0,
            )
            with self.assertRaisesRegex(
                AuthenticationExpiredError,
                "redirect loop detected",
            ):
                fetch_job(
                    state,
                    run_id=run_id,
                    job=job,
                    session=RedirectLoopSession(),
                    rate_limiter=NoWait(),
                    snapshot_root=Path(temp_dir),
                    refresh=False,
                )
            state.close()


if __name__ == "__main__":
    unittest.main()
