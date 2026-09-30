from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from backend.database import (
    complete_pipeline_run,
    fetch_dashboard,
    fetch_pipeline_run,
    fetch_pipeline_source_version,
    initialize_database,
    insert_pipeline_run,
    save_pipeline_source_version,
)
from backend.pipelines import v3
from backend.pipelines.v3_source_check import _canonical_csv, surveycto_source_fingerprint


class V3SourceFingerprintTests(unittest.TestCase):
    def test_canonical_csv_detects_values_and_duplicates_but_ignores_order(self):
        before, count = _canonical_csv(b"id,value\n1,a\n2,b\n", "source")
        reordered, reordered_count = _canonical_csv(b"value,id\nb,2\na,1\n", "source")
        edited, _ = _canonical_csv(b"id,value\n1,a\n2,c\n", "source")
        duplicate, _ = _canonical_csv(b"id,value\n1,a\n2,b\n2,b\n", "source")
        self.assertEqual((count, reordered_count), (2, 2))
        self.assertEqual(before, reordered)
        self.assertNotEqual(before, edited)
        self.assertNotEqual(before, duplicate)

    def test_downloads_the_same_three_sources_used_by_v3(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text(
                "SURVEYCTO_SERVER=alpmetrics\n"
                "SURVEYCTO_USERNAME=portal@example.com\n"
                "SURVEYCTO_PASSWORD=secret\n"
            )
            calls = []

            def request_get(url, **kwargs):
                calls.append((url, kwargs))
                response = Mock()
                response.content = b"KEY,value\nuuid:1,a\n"
                response.raise_for_status.return_value = None
                return response

            result = surveycto_source_fingerprint(root, request_get=request_get)
            self.assertEqual(len(result["sources"]), 3)
            self.assertEqual([item["rowCount"] for item in result["sources"]], [1, 1, 1])
            self.assertEqual(calls[0][1]["auth"], ("portal@example.com", "secret"))
            self.assertEqual(calls[0][1]["timeout"], 120)
            self.assertIn("/api/v1/forms/data/wide/csv/alp_metrics_survey_v3", calls[0][0])
            self.assertIn("/api/v1/forms/data/wide/csv/alp_metrics_survey_v3_promat", calls[1][0])
            self.assertIn("/api/v2/datasets/data/csv/alp_metrics_survey_v3_wide_historic_data", calls[2][0])


class V3PreflightIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "portal.db"
        initialize_database(self.db)
        self.repo = {
            "root": str(self.root),
            "commit": "abc123",
            "branch": "main",
            "isDirty": False,
        }

    def _prior_version(self, fingerprint="same", commit="abc123"):
        run_id = insert_pipeline_run(
            self.db,
            status="running",
            extract_mode="surveycto",
            started_at="2026-09-28T10:00:00+00:00",
            triggered_by_email=None,
            triggered_by_name="Previous",
            pipeline_version="V3",
        )
        complete_pipeline_run(
            self.db,
            run_id=run_id,
            status="completed",
            completed_at="2026-09-28T10:01:00+00:00",
            message="Done",
            run_log="Previous full pipeline log",
        )
        save_pipeline_source_version(
            self.db,
            pipeline_version="V3",
            fingerprint=fingerprint,
            pipeline_commit=commit,
            run_id=run_id,
            updated_at="2026-09-28T10:01:00+00:00",
        )
        return run_id

    def test_unchanged_source_skips_pipeline_upload_snapshot_and_refresh(self):
        prior_id = self._prior_version()
        source = {"fingerprint": "same", "sources": [{}, {}, {}]}
        with (
            patch.object(v3, "get_pipeline_repo_status", return_value=self.repo),
            patch.object(v3, "surveycto_source_fingerprint", return_value=source),
            patch.object(v3, "_build_pipeline_config") as build,
            patch.object(v3, "_upload_export_files") as upload,
        ):
            result = v3.run_pipeline_and_snapshot(
                self.db,
                upload_to_sharepoint=True,
                publish_snapshot=True,
            )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "unchanged_surveycto_data")
        build.assert_not_called()
        upload.assert_not_called()
        run = fetch_pipeline_run(self.db, result["run_id"])
        self.assertEqual(run["status"], "skipped")
        self.assertEqual(run["message"], "No SurveyCTO changes; pipeline and uploads skipped.")
        self.assertIsNone(run["run_log"])
        dashboard = fetch_dashboard(self.db)
        self.assertEqual(dashboard["latest_runs"]["V3"]["id"], result["run_id"])
        visible_pipeline = dashboard["latest_pipeline_runs"]["V3"]
        self.assertEqual(visible_pipeline["id"], prior_id)
        self.assertEqual(visible_pipeline["run_log"], "Previous full pipeline log")

    def test_database_migrates_an_existing_preflight_log_to_a_silent_skip(self):
        prior_id = self._prior_version()
        legacy_id = insert_pipeline_run(
            self.db,
            status="running",
            extract_mode="surveycto",
            started_at="2026-09-28T11:00:00+00:00",
            triggered_by_email=None,
            triggered_by_name="Automatic schedule",
            pipeline_version="V3",
        )
        complete_pipeline_run(
            self.db,
            run_id=legacy_id,
            status="completed",
            completed_at="2026-09-28T11:01:00+00:00",
            message="No SurveyCTO changes; pipeline and uploads skipped.",
            run_log="SurveyCTO preflight checked 3 V3 sources; no changes detected.",
        )
        initialize_database(self.db)
        migrated = fetch_pipeline_run(self.db, legacy_id)
        self.assertEqual(migrated["status"], "skipped")
        self.assertIsNone(migrated["run_log"])
        dashboard = fetch_dashboard(self.db)
        self.assertEqual(dashboard["latest_runs"]["V3"]["id"], legacy_id)
        self.assertEqual(dashboard["latest_pipeline_runs"]["V3"]["id"], prior_id)

    def test_forced_run_ignores_an_unchanged_source_fingerprint(self):
        self._prior_version()
        export = self.root / "final.csv"
        pd.DataFrame([{"project": "Project", "SubmissionDate": "2026-09-28"}]).to_csv(export, index=False)
        config = SimpleNamespace(
            root_dir=self.root,
            processed_csv_path="final.csv",
            labeled_csv_path="labelled.csv",
            exports_dir="files/pipeline",
        )
        with (
            patch.object(v3, "get_pipeline_repo_status", return_value=self.repo),
            patch.object(v3, "surveycto_source_fingerprint", return_value={
                "fingerprint": "same", "sources": [{}, {}, {}],
            }) as source_check,
            patch.object(v3, "_build_pipeline_config", return_value=config),
            patch.object(v3, "_run_pipeline_with_log_capture", return_value="Forced pipeline log") as run,
            patch.object(v3, "_upload_export_files", return_value=[]),
        ):
            result = v3.run_pipeline_and_snapshot(
                self.db,
                force_run=True,
                upload_to_sharepoint=True,
                publish_snapshot=True,
            )
        source_check.assert_called_once()
        run.assert_called_once()
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("skipped", result)

    def test_successful_publish_saves_source_version_for_the_next_run(self):
        export = self.root / "final.csv"
        pd.DataFrame([{"project": "Project", "SubmissionDate": "2026-09-28"}]).to_csv(export, index=False)
        config = SimpleNamespace(
            root_dir=self.root,
            processed_csv_path="final.csv",
            labeled_csv_path="labelled.csv",
            exports_dir="files/pipeline",
        )
        upload_row = {
            "file_name": "final.csv",
            "local_path": str(export),
            "sharepoint_path": "alp-metrics-pipeline/final.csv",
            "status": "uploaded",
            "uploaded_at": "2026-09-28T10:00:00+00:00",
            "web_url": "https://example/final.csv",
            "message": "Uploaded",
        }
        source = {"fingerprint": "new", "sources": [{}, {}, {}]}
        with (
            patch.object(v3, "get_pipeline_repo_status", return_value=self.repo),
            patch.object(v3, "surveycto_source_fingerprint", return_value=source),
            patch.object(v3, "_build_pipeline_config", return_value=config),
            patch.object(v3, "_run_pipeline_with_log_capture", return_value="Pipeline ran"),
            patch.object(v3, "_upload_export_files", return_value=[upload_row]),
        ):
            result = v3.run_pipeline_and_snapshot(
                self.db,
                upload_to_sharepoint=True,
                publish_snapshot=True,
            )
        saved = fetch_pipeline_source_version(self.db, "V3")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(saved["fingerprint"], "new")
        self.assertEqual(saved["pipeline_commit"], "abc123")
        self.assertEqual(saved["run_id"], result["run_id"])

    def test_failed_upload_does_not_advance_source_version(self):
        self._prior_version(fingerprint="old")
        export = self.root / "final.csv"
        pd.DataFrame([{"project": "Project", "SubmissionDate": "2026-09-28"}]).to_csv(export, index=False)
        config = SimpleNamespace(
            root_dir=self.root,
            processed_csv_path="final.csv",
            labeled_csv_path="labelled.csv",
            exports_dir="files/pipeline",
        )
        failed_upload = {
            "file_name": "final.csv",
            "local_path": str(export),
            "sharepoint_path": "alp-metrics-pipeline/final.csv",
            "status": "failed",
            "uploaded_at": None,
            "web_url": None,
            "message": "Upload failed",
        }
        source = {"fingerprint": "new", "sources": [{}, {}, {}]}
        with (
            patch.object(v3, "get_pipeline_repo_status", return_value=self.repo),
            patch.object(v3, "surveycto_source_fingerprint", return_value=source),
            patch.object(v3, "_build_pipeline_config", return_value=config),
            patch.object(v3, "_run_pipeline_with_log_capture", return_value="Pipeline ran"),
            patch.object(v3, "_upload_export_files", return_value=[failed_upload]),
        ):
            v3.run_pipeline_and_snapshot(
                self.db,
                upload_to_sharepoint=True,
                publish_snapshot=True,
            )
        self.assertEqual(fetch_pipeline_source_version(self.db, "V3")["fingerprint"], "old")


if __name__ == "__main__":
    unittest.main()
