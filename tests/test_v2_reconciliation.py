from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from backend import auto_refresh as auto
from backend.database import (connect_database, fetch_dashboard, initialize_database,
                              insert_pipeline_run, publish_run_snapshot)
from backend.pipelines import v2
from backend.pipelines.snapshots import build_snapshot_dataframe


def job(name, survey="Survey"):
    return {"type": "farmer", "form_id": "form", "project_name": name, "survey_name": survey}


class V2ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "portal.db"
        initialize_database(self.db)
        (self.root / "main.py").write_text("# fixture runner\n")

    def _run_id(self, version):
        return insert_pipeline_run(self.db, status="completed", extract_mode="configured",
                                   started_at="2026-11-04T10:00:00Z", pipeline_version=version,
                                   triggered_by_email=None, triggered_by_name=None)

    def _export(self, output, item):
        rows = [{"project": item["project_name"], "SubmissionDate": "2026-11-01",
                 "phase_pl": "Baseline", "enumerator": "E"}]
        relative = Path(item["project_name"]) / item["survey_name"] / "data" / v2.PROCESSED_EXPORT_NAMES[item["type"]]
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(path, index=False)
        return rows, path, relative.as_posix()

    def seed(self, item, version="V2"):
        rows, path, relative = self._export(self.root / "previous", item)
        summaries, _ = build_snapshot_dataframe(pd.DataFrame(rows))
        for summary in summaries:
            summary.update(source_key=v2.source_key(item), instance_key=v2.source_key(item),
                           project_key=v2.project_key(item), source_survey=item["survey_name"])
        upload = {"file_name": path.name, "local_path": str(path), "relative_path": relative,
                  "source_key": v2.source_key(item), "project_key": v2.project_key(item),
                  "sharepoint_path": f"V2/{relative}", "is_project_data": True,
                  "status": "uploaded", "uploaded_at": "2026-11-01", "web_url": "https://example/file"}
        publish_run_snapshot(self.db, run_id=self._run_id(version), pipeline_version=version,
                             survey_rows=summaries, record_rows=[], upload_rows=[upload],
                             source_keys=[v2.source_key(item)])
        return path

    def enable_auto_with_current_watermark(self):
        config = {"reportIds": ["report"], "workspaceId": "workspace",
                  "reports": [{"id": "report", "name": "Report", "datasetId": "dataset"}]}
        with connect_database(self.db) as connection:
            uploads = [dict(row) for row in connection.execute(
                "SELECT * FROM pipeline_uploads WHERE pipeline_version='V2'").fetchall()]
            connection.execute("INSERT OR REPLACE INTO powerbi_auto_settings VALUES (1,?)", (json.dumps(config),))
        fingerprint = auto.data_update_fingerprint("V2", uploads, allow_empty=True)
        with connect_database(self.db) as connection:
            connection.execute("INSERT OR REPLACE INTO powerbi_refresh_watermarks VALUES (?,?,?,?)",
                               ("workspace", "dataset", "V2", fingerprint))

    def execute(self, configured, entries, *, returncode=0, scope="full", upload_status="completed"):
        def run(command, **kwargs):
            output = Path(command[command.index("--output") + 1])
            uploads = []
            for entry in entries:
                if entry.get("success") and not v2.is_empty_job(entry):
                    _, path, relative = self._export(output, entry["job"])
                    uploads.append({"relative_path": relative, "status": "uploaded" if upload_status == "completed" else "failed",
                                    "web_url": "https://example/file", "folder_web_url": "https://example/data"})
            manifest = {"jobs": deepcopy(entries), "bootstrap": {"status": "completed"},
                        "raw_project_exports": {"status": "completed", "files": []},
                        "sharepoint": {"status": upload_status, "files": uploads}}
            if scope is not None:
                manifest.update(configuration_scope=scope, configured_jobs=configured)
            Path(command[command.index("--manifest") + 1]).write_text(json.dumps(manifest))
            return SimpleNamespace(returncode=returncode)

        with patch.object(v2, "pipeline_root", return_value=self.root), \
             patch.object(v2, "get_pipeline_repo_status", return_value={}), \
             patch.object(v2, "subprocess") as subprocess:
            subprocess.run.side_effect = run
            return v2.run_pipeline_and_snapshot(self.db)

    def sources(self, table="survey_summaries", version="V2"):
        with connect_database(self.db) as connection:
            return {row[0] for row in connection.execute(
                f"SELECT source_key FROM {table} WHERE pipeline_version=?", (version,)).fetchall()}

    def test_removal_is_compared_before_atomic_database_cleanup_and_queues_refresh(self):
        keep, remove = job("Keep"), job("Remove")
        self.seed(keep)
        old_file = self.seed(remove)
        self.seed(job("V3"), version="V3")
        self.enable_auto_with_current_watermark()
        calculate = auto.data_update_fingerprint
        compared = []

        def check_before_cleanup(*args, **kwargs):
            self.assertIn(v2.source_key(remove), self.sources())
            self.assertIn(v2.source_key(remove), self.sources("pipeline_uploads"))
            compared.append(True)
            return calculate(*args, **kwargs)

        with patch.object(auto, "data_update_fingerprint", side_effect=check_before_cleanup):
            result = self.execute([keep], [{"job": keep, "success": True}])
        self.assertEqual(compared, [True])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["removed_survey_count"], 1)
        self.assertEqual(self.sources(), {v2.source_key(keep)})
        self.assertEqual(self.sources("pipeline_uploads"), {v2.source_key(keep)})
        self.assertEqual(self.sources(version="V3"), {v2.source_key(job("V3"))})
        self.assertTrue(old_file.exists())
        self.assertEqual(auto.latest_job(self.db)["status"], "queued")

    def test_configured_empty_survey_does_not_create_a_false_removal_refresh(self):
        empty = job("Empty")
        self.seed(empty)
        self.enable_auto_with_current_watermark()
        skipped = {"job": empty, "success": True,
                   "result": {"status": "skipped", "reason": "no_project_records", "Data": []}}
        self.execute([empty], [skipped])
        self.assertEqual(self.sources(), {v2.source_key(empty)})
        self.assertEqual(auto.latest_job(self.db)["status"], "skipped")

    def test_unchanged_full_configuration_does_not_queue_refresh(self):
        keep = job("Keep")
        self.seed(keep)
        self.enable_auto_with_current_watermark()
        self.execute([keep], [{"job": keep, "success": True}])
        self.assertEqual(auto.latest_job(self.db)["status"], "skipped")

    def test_empty_configuration_removes_last_v2_survey_and_queues_empty_fingerprint(self):
        self.seed(job("Remove"))
        self.seed(job("V3"), version="V3")
        self.enable_auto_with_current_watermark()
        result = self.execute([], [])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.sources(), set())
        self.assertEqual(self.sources("pipeline_uploads"), set())
        self.assertEqual(self.sources(version="V3"), {v2.source_key(job("V3"))})
        self.assertEqual(auto.latest_job(self.db)["status"], "queued")
        with connect_database(self.db) as connection:
            fingerprint = connection.execute("SELECT fingerprint FROM powerbi_data_versions WHERE version='V2'").fetchone()[0]
            connection.execute("INSERT OR REPLACE INTO powerbi_refresh_watermarks VALUES (?,?,?,?)",
                               ("workspace", "dataset", "V2", fingerprint))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0], 3)
        self.execute([], [])
        self.assertEqual(auto.latest_job(self.db)["status"], "skipped")

    def test_configured_empty_survey_is_retained_while_unconfigured_survey_is_removed(self):
        empty, removed = job("Empty"), job("Removed")
        old_file = self.seed(empty)
        self.seed(removed)
        self.enable_auto_with_current_watermark()
        skipped = {"job": empty, "success": True,
                   "result": {"status": "skipped", "reason": "no_project_records", "Data": []}}
        self.execute([empty], [skipped])
        self.assertEqual(self.sources(), {v2.source_key(empty)})
        self.assertTrue(old_file.exists())
        self.assertEqual(auto.latest_job(self.db)["status"], "queued")

    def test_partial_or_failed_runs_do_not_reconcile_unconfigured_surveys(self):
        keep, removed, new = job("Failed configured"), job("Removed"), job("New")
        self.seed(keep)
        self.seed(removed)
        self.enable_auto_with_current_watermark()
        result = self.execute([keep, new], [{"job": keep, "success": False}, {"job": new, "success": True}], returncode=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.sources(), {v2.source_key(item) for item in (keep, removed, new)})
        self.assertIsNone(auto.latest_job(self.db))

    def test_upload_failure_does_not_remove_old_surveys_or_queue_refresh(self):
        keep, removed = job("Keep"), job("Removed")
        self.seed(keep)
        self.seed(removed)
        self.enable_auto_with_current_watermark()
        result = self.execute([keep], [{"job": keep, "success": True}], returncode=1, upload_status="failed")
        self.assertEqual(result["status"], "partial")
        self.assertIn(v2.source_key(removed), self.sources())
        self.assertIsNone(auto.latest_job(self.db))

    def test_partial_scope_and_legacy_manifests_preserve_unmentioned_surveys(self):
        keep, removed = job("Keep"), job("Removed")
        self.seed(keep)
        self.seed(removed)
        for scope in ("partial", None):
            with self.subTest(scope=scope):
                self.execute([keep], [{"job": keep, "success": True}], scope=scope)
                self.assertIn(v2.source_key(removed), self.sources())

    def test_fingerprint_error_leaves_all_previous_snapshots_and_files_intact(self):
        old, new = job("Old"), job("New")
        self.seed(old)
        self.enable_auto_with_current_watermark()
        with patch.object(auto, "data_update_fingerprint", side_effect=ValueError("Invalid CSV")):
            with self.assertRaisesRegex(ValueError, "Invalid CSV"):
                self.execute([new], [{"job": new, "success": True}])
        self.assertEqual(self.sources(), {v2.source_key(old)})
        self.assertEqual(self.sources("pipeline_uploads"), {v2.source_key(old)})
        self.assertIsNone(auto.latest_job(self.db))

    def test_renaming_a_survey_retires_the_old_portal_instance(self):
        old, new = job("Project", "Old survey"), job("Project", "New survey")
        self.seed(old)
        result = self.execute([new], [{"job": new, "success": True}])
        self.assertEqual(result["removed_survey_count"], 1)
        self.assertEqual(self.sources(), {v2.source_key(new)})

    def test_inconsistent_configured_jobs_cannot_delete_database_records(self):
        old, produced = job("Old"), job("Unexpected")
        self.seed(old)
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.execute([], [{"job": produced, "success": True}])
        self.assertEqual(self.sources(), {v2.source_key(old)})


if __name__ == "__main__":
    unittest.main()
