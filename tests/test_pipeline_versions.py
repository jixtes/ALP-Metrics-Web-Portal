import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from flask_security import SQLAlchemyUserDatastore, hash_password

from backend.app import create_app, _filter_project_file_uploads
from backend.auth import db as auth_db, User, Role
from backend.database import (initialize_database, insert_pipeline_run, publish_run_snapshot,
                              fetch_dashboard, fetch_pipeline_run, complete_pipeline_run)
from backend.pipelines import v2, v3
from backend.pipelines.snapshots import build_snapshot_dataframe
from backend import service


def survey(name="Project", **extra):
    rows, _ = build_snapshot_dataframe(pd.DataFrame([
        {"project": name, "SubmissionDate": "2026-09-01", "enumerator": "Enumerator A",
         "country_pl": "Ethiopia", "phase_pl": "Baseline", "client_pl": "Client"}
    ]))
    return {**rows[0], **extra}


def upload(name="result.csv", **extra):
    return {"file_name": name, "local_path": "/exports/" + name,
            "sharepoint_path": "pipeline/project_data/" + name, "status": "uploaded",
            "uploaded_at": "2026-09-01", "web_url": "https://example/file", "message": "Uploaded", **extra}


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "portal.db"
        initialize_database(self.db)

    def run_id(self, version="V3"):
        return insert_pipeline_run(self.db, status="running", extract_mode="surveycto" if version == "V3" else "configured",
                                   pipeline_version=version, started_at="2026-09-22", triggered_by_email=None,
                                   triggered_by_name=None)

    def publish(self, version, rows, files, source_keys=None):
        run_id = self.run_id(version)
        publish_run_snapshot(self.db, run_id=run_id, pipeline_version=version,
                             survey_rows=rows, record_rows=[], upload_rows=files, source_keys=source_keys)
        return run_id

class SnapshotStorageTests(SnapshotFixture):
    def test_versions_and_failed_v2_projects_survive_other_updates(self):
        self.publish("V3", [survey()], [upload("v3.csv")])
        self.publish("V2", [survey(source_key="a", project_key="V2:Project"), survey("Other", source_key="b")],
                     [upload("a.csv", source_key="a"), upload("b.csv", source_key="b")])
        self.publish("V2", [survey(source_key="a", submission_count=7)], [upload("new-a.csv", source_key="a")], ["a"])
        current = fetch_dashboard(self.db)
        self.assertEqual(len(current["surveys"]), 3)
        self.assertEqual({f["file_name"] for f in current["uploads"]}, {"v3.csv", "new-a.csv", "b.csv"})
        self.publish("V3", [survey("New V3")], [upload("new-v3.csv")])
        current = fetch_dashboard(self.db)
        self.assertEqual(len([s for s in current["surveys"] if s["pipeline_version"] == "V2"]), 2)
        self.assertEqual({f["file_name"] for f in current["uploads"]}, {"new-v3.csv", "new-a.csv", "b.csv"})
        self.assertEqual(set(current["latest_runs"]), {"V2", "V3"})

    def test_snapshot_and_files_roll_back_together(self):
        self.publish("V3", [survey()], [upload()])
        with self.assertRaises(sqlite3.IntegrityError):
            self.publish("V3", [survey("Replacement")], [upload(local_path=None)])
        current = fetch_dashboard(self.db)
        self.assertEqual(current["surveys"][0]["survey_name"], "Project")
        self.assertEqual(len(current["uploads"]), 1)

    def test_migration_backfills_existing_data_as_v3(self):
        self.publish("V3", [survey()], [upload()])
        with sqlite3.connect(self.db) as conn:
            for table in ("pipeline_runs", "survey_summaries", "survey_records", "pipeline_uploads"):
                conn.execute(f"ALTER TABLE {table} DROP COLUMN pipeline_version")
            for table in ("survey_summaries", "pipeline_uploads"):
                for column in ("project_key", "source_key"):
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        initialize_database(self.db)
        initialize_database(self.db)
        current = fetch_dashboard(self.db)
        self.assertEqual(current["surveys"][0]["pipeline_version"], "V3")
        self.assertEqual(current["surveys"][0]["project_key"], "Project")
        self.assertEqual(current["uploads"][0]["pipeline_version"], "V3")

    def test_run_stores_snapshot_counts(self):
        run_id = self.publish("V2", [survey(submission_count=4)], [])
        complete_pipeline_run(self.db, run_id=run_id, status="completed", completed_at="2026-09-22", message="Done")
        run = fetch_pipeline_run(self.db, run_id)
        self.assertEqual((run["row_count"], run["survey_count"], run["pipeline_version"]), (4, 1, "V2"))


class V2IntegrationTests(SnapshotFixture):
    def test_empty_job_keeps_existing_snapshot_and_completes_with_explanation(self):
        job = {"type": "retailer", "project_name": "Project", "survey_name": "Retailer"}
        self.publish("V2", [survey(source_key=v2.source_key(job), project_key="V2:Project")],
                     [upload(source_key=v2.source_key(job))])
        entry = {"job": job, "success": True,
                 "result": {"status": "skipped", "reason": "no_project_records", "Data": []}}
        manifest = {"jobs": [entry], "sharepoint": {"status": "completed", "files": []}}
        self.assertEqual(v2.collect_snapshots(manifest, self.root), ([], [], [], []))
        self.assertFalse(v2.is_empty_job({**entry, "success": False}))
        (self.root / "main.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            f"Path(sys.argv[sys.argv.index('--manifest')+1]).write_text({json.dumps(manifest)!r})\n")
        with patch.object(v2, "pipeline_root", return_value=self.root), patch.dict(os.environ, {"ALP_V2_PYTHON": sys.executable}):
            result = v2.run_pipeline_and_snapshot(self.db)
        self.assertEqual(result["status"], "completed")
        self.assertIn("no matching project records", result["message"])
        current = fetch_dashboard(self.db)
        self.assertEqual(len(current["surveys"]), 1)
        self.assertEqual(len(current["uploads"]), 1)

    def export(self, job, rows=None):
        folder = self.root / "output" / job["project_name"] / job["survey_name"] / "data"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "ALP_Farmer_FullProcessedDataWithLabels.csv"
        pd.DataFrame(rows or [{"project": job["project_name"], "SubmissionDate": "2026-09-01",
                              "phase_pl": "Baseline", "enumerator": "E", "country_pl": "Ethiopia",
                              "ifcproject_pl": "Project reference", "mfid_key": "id-1"}]).to_csv(path, index=False)
        return path

    def test_v2_fields_and_phase_instances_map_to_overview(self):
        job = {"project_name": "Project", "survey_name": "Farmer survey", "type": "farmer"}
        self.export(job, [{"project": "Project", "phase_pl": phase, "SubmissionDate": "2026-09-01",
                           "enumerator": "E", "client_pl": "Client", "country_pl": "Ethiopia"}
                          for phase in ["Baseline", "Endline", "Endline"]])
        rows = v2.build_job_snapshot(self.root / "output", job, "https://example/data")
        self.assertEqual(sorted(r["submission_count"] for r in rows), [1, 2])
        self.assertEqual(len({r["instance_key"] for r in rows}), 2)
        self.assertEqual(rows[0]["project_key"], "V2:Project")
        self.assertEqual(rows[0]["data_folder_url"], "https://example/data")
        self.assertEqual(rows[0]["preview"]["active_enumerator_count"], 1)
        self.assertEqual(rows[0]["preview"]["entity_category_counts"]["lead_farmers"], rows[0]["submission_count"])

    def test_failed_or_mismatched_outputs_do_not_refresh_existing_snapshot(self):
        job = {"project_name": "Project", "survey_name": "Farmer survey", "type": "farmer"}
        self.export(job, [{"project": "Wrong project", "phase_pl": "Baseline", "SubmissionDate": "2026-09-01"}])
        manifest = {"jobs": [{"job": job, "success": True}, {"job": {**job, "project_name": "Failed"}, "success": False}]}
        rows, files, refreshed, errors = v2.collect_snapshots(manifest, self.root / "output")
        self.assertEqual((rows, files, refreshed), ([], [], []))
        self.assertEqual(len(errors), 2)

    def test_real_subprocess_runner_publishes_partial_results_and_keeps_other_sources(self):
        failed_job = {"type": "retailer", "project_name": "Other", "survey_name": "Retailer survey"}
        self.publish("V3", [survey("V3 project")], [upload("v3.csv")])
        self.publish("V2", [survey("Other", source_key=v2.source_key(failed_job), project_key="V2:Other")], [])
        # A real child process writes the same output/manifest contract as V2,
        # without contacting SurveyCTO or SharePoint.
        (self.root / "main.py").write_text('''import argparse, csv, json
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--output'); p.add_argument('--manifest'); p.add_argument('--skip-sharepoint',action='store_true')
a=p.parse_args()
out=Path(a.output)
path=out/'Project'/'Farmer survey'/'data'/'ALP_Farmer_FullProcessedDataWithLabels.csv'
path.parent.mkdir(parents=True)
with path.open('w') as f:
    writer=csv.writer(f); writer.writerow(['project','SubmissionDate','phase_pl','enumerator'])
    writer.writerow(['Project','2026-09-01','Baseline','E'])
job={'project_name':'Project','survey_name':'Farmer survey','type':'farmer'}
failed={'project_name':'Other','survey_name':'Retailer survey','type':'retailer'}
manifest={'jobs':[{'job':job,'success':True},{'job':failed,'success':False}],
          'sharepoint':{'status':'completed','files':[{'relative_path':path.relative_to(out).as_posix(),
          'status':'uploaded','web_url':'https://example/file','folder_web_url':'https://example/data'}]}}
Path(a.manifest).write_text(json.dumps(manifest))
print('Finished fixture pipeline')
raise SystemExit(1)
''')
        with patch.object(v2, "pipeline_root", return_value=self.root), patch.dict(os.environ, {"ALP_V2_PYTHON": sys.executable}):
            result = v2.run_pipeline_and_snapshot(self.db)
        self.assertEqual(result["status"], "partial")
        current = fetch_dashboard(self.db)
        self.assertEqual({s["survey_name"] for s in current["surveys"]}, {"V3 project", "Project", "Other"})
        added = next(s for s in current["surveys"] if s["survey_name"] == "Project")
        self.assertEqual(added["submission_count"], 1)
        self.assertEqual(added["data_folder_url"], "https://example/data")
        self.assertEqual(current["latest_runs"]["V2"]["row_count"], 1)
        self.assertIn("Finished fixture pipeline", current["latest_runs"]["V2"]["run_log"])

    def test_v3_adapter_preserves_v2_snapshots(self):
        self.publish("V2", [survey("V2 project")], [])
        export = self.root / "final.csv"
        pd.DataFrame([{"project": "V3 project", "SubmissionDate": "2026-09-01"}]).to_csv(export, index=False)
        config = SimpleNamespace(root_dir=self.root, processed_csv_path="final.csv", labeled_csv_path="labelled.csv", exports_dir="files/pipeline")
        with patch.object(v3, "_build_pipeline_config", return_value=config), patch.object(v3, "_run_pipeline_with_log_capture", return_value="V3 log"), patch.object(v3, "get_pipeline_repo_status", return_value={}):
            v3.run_pipeline_and_snapshot(self.db)
        current = fetch_dashboard(self.db)
        self.assertEqual({s["pipeline_version"] for s in current["surveys"]}, {"V2", "V3"})


class VersionRoutingTests(unittest.TestCase):
    def test_normal_v3_runs_force_surveycto_and_webhook_keeps_test_mode(self):
        with patch.object(service.v3, "run_pipeline_and_snapshot") as run:
            service.run_pipeline_and_snapshot(Path("test.db"), extract_mode="csv")
            self.assertEqual(run.call_args.kwargs["extract_mode"], "surveycto")
            service.run_pipeline_and_snapshot(Path("test.db"), extract_mode="surveycto_test", publish_snapshot=False)
            self.assertEqual(run.call_args.kwargs["extract_mode"], "surveycto_test")

    def test_project_file_access_does_not_cross_versions_or_expose_raw_data(self):
        v2_file = upload(pipeline_version="V2", project_key="V2:Project", relative_path="Project/Survey/data/file.csv", is_project_data=1)
        v2_raw = upload(pipeline_version="V2", project_key="V2:Project", relative_path="Project/Survey/raw/all/raw.csv", is_project_data=0)
        v3_file = upload("project.csv", pipeline_version="V3")
        surveys = [survey(project_key="Project", pipeline_version="V3"), survey(project_key="V2:Project", pipeline_version="V2")]
        files = [v2_file, v2_raw, v3_file]
        self.assertEqual(_filter_project_file_uploads(files, surveys, "restricted", {"V2:Project"}), [v2_file])
        self.assertEqual(_filter_project_file_uploads(files, surveys, "restricted", {"Project"}), [v3_file])
        self.assertEqual(_filter_project_file_uploads(files, surveys, "all", set()), [v2_file, v3_file])


class PipelineAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ALP_INITIAL_ADMIN_EMAIL": "pipeline-test@example.com",
                                          "ALP_INITIAL_ADMIN_PASSWORD": "test-password-123"})
        self.env.start()
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False,
                               "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.root / 'auth.db'}",
                               "DATABASE_PATH": str(self.root / "portal.db")})
        self.client = self.app.test_client()
        with patch("backend.auth._require_csrf"):
            self.assertEqual(self.client.post("/api/auth/login", json={"email": "pipeline-test@example.com", "password": "test-password-123"}).status_code, 200)

    def tearDown(self):
        with self.app.app_context():
            auth_db.session.remove()
            auth_db.engine.dispose()
        self.env.stop()
        self.temp.cleanup()

    def test_version_selector_controls_runner_and_rejects_invalid_versions(self):
        with patch("backend.app.Thread") as thread, patch("backend.app.get_pipeline_repo_status", return_value={}) as repo:
            for version in ("V2", "V3"):
                response = self.client.post("/api/pipeline/run", json={"pipelineVersion": version, "extractMode": "csv"})
                self.assertEqual(response.status_code, 202)
                kwargs = thread.call_args.kwargs["kwargs"]
                self.assertEqual(kwargs["pipeline_version"], version)
                self.assertEqual(kwargs["extract_mode"], "surveycto" if version == "V3" else "configured")
                repo.assert_called_with(version)
                run = self.client.get(f"/api/pipeline/runs/{response.json['run_id']}").json
                self.assertEqual(run["pipeline_version"], version)
            response = self.client.post("/api/pipeline/run", json={"pipelineVersion": "V1"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(thread.call_count, 2)

    def test_status_and_pull_select_the_correct_repository(self):
        with patch("backend.app.get_pipeline_repo_status", return_value={}) as status, patch("backend.app.pull_pipeline_repo", return_value={"status": "completed"}) as pull:
            self.assertEqual(self.client.get("/api/pipeline/status?pipelineVersion=V2").status_code, 200)
            status.assert_called_once_with("V2")
            self.assertEqual(self.client.post("/api/pipeline/pull", json={"pipelineVersion": "V2"}).status_code, 200)
            pull.assert_called_once_with("V2")

    def test_restricted_dashboard_only_returns_authorized_v2_data_and_hides_logs(self):
        db_path = self.root / "portal.db"
        for version in ("V2", "V3"):
            run_id = insert_pipeline_run(db_path, status="completed", extract_mode="configured", pipeline_version=version,
                                         started_at="2026-09-22", triggered_by_email=None, triggered_by_name=None)
            project = "V2:Project" if version == "V2" else "Project"
            rows = [survey(project_key=project, data_folder_url="https://example/data")]
            files = [upload("project.csv", pipeline_version=version, project_key=project,
                            relative_path="Project/Survey/data/file.csv", is_project_data=1)]
            if version == "V2":
                files.append(upload("raw.csv", project_key=project, relative_path="Project/Survey/raw/all/raw.csv"))
            publish_run_snapshot(db_path, run_id=run_id, pipeline_version=version, survey_rows=rows, record_rows=[], upload_rows=files)
            complete_pipeline_run(db_path, run_id=run_id, status="completed", completed_at="2026-09-22", message="Done", run_log="private diagnostic rows")
        with self.app.app_context():
            store = SQLAlchemyUserDatastore(auth_db, User, Role)
            role = store.create_role(name="v2-client", project_scope="restricted", upload_scope="project_files")
            store.create_user(email="client@example.com", password=hash_password("client-test-password"), active=True,
                              roles=[role], allowed_project_refs_json=json.dumps(["V2:Project"]))
            auth_db.session.commit()
        client = self.app.test_client()
        with patch("backend.auth._require_csrf"):
            self.assertEqual(client.post("/api/auth/login", json={"email": "client@example.com", "password": "client-test-password"}).status_code, 200)
        response = client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["pipeline_version"] for row in response.json["surveys"]], ["V2"])
        self.assertEqual([row["file_name"] for row in response.json["uploads"]], ["project.csv"])
        for run in response.json["latest_runs"].values():
            self.assertNotIn("run_log", run)
        self.assertNotIn("run_log", client.get(f"/api/pipeline/runs/{run_id}").json)
        with self.app.app_context():
            Role.query.filter_by(name="v2-client").first().upload_scope = "none"
            auth_db.session.commit()
        response = client.get("/api/dashboard")
        self.assertEqual(response.json["uploads"], [])
        self.assertIsNone(response.json["surveys"][0]["data_folder_url"])


if __name__ == "__main__":
    unittest.main()
