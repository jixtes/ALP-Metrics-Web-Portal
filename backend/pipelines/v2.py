"""Run the standalone V2 checkout and adapt its exports for the portal."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Any
from uuid import uuid4

from dotenv import dotenv_values
import pandas as pd

from ..database import complete_pipeline_run, insert_pipeline_run, publish_run_snapshot
from .repository import RUN_LOCK, pipeline_root, repo_status, commit_details, pull_repo
from .snapshots import SUMMARY_COLUMNS, build_snapshot_dataframe

ENTITY_TYPES = {"farmer": "Lead farmer", "retailer": "Retailer", "po": "Producer organisation", "sme": "SME"}


def get_pipeline_repo_status() -> dict:
    return repo_status(pipeline_root("V2"), "V2")


def get_pipeline_commit_details(commit: str | None) -> dict:
    return commit_details(pipeline_root("V2"), commit)


def pull_pipeline_repo() -> dict:
    return pull_repo(pipeline_root("V2"), "V2")


def python_executable(root: Path) -> str:
    configured = os.getenv("ALP_V2_PYTHON", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path if path.is_absolute() else (root / path).resolve())
    for relative in (".runner-venv/bin/python", ".runner-venv/Scripts/python.exe"):
        candidate = root / relative
        if candidate.exists():
            return str(candidate)
    return sys.executable


def source_key(job: dict) -> str:
    return json.dumps([job["type"], job["project_name"], job["survey_name"]], ensure_ascii=False)


def project_key(job: dict) -> str:
    return "V2:" + job["project_name"]


def job_directory(output_dir: Path, job: dict) -> Path:
    for key in ("project_name", "survey_name"):
        name = job[key]
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError(f"Invalid V2 {key} for an output folder.")
    result = (output_dir / job["project_name"] / job["survey_name"]).resolve()
    if not result.is_relative_to(output_dir.resolve()):
        raise ValueError("V2 project output is outside this run's output directory.")
    return result


def build_job_snapshot(output_dir: Path, job: dict, folder_url: str | None = None) -> list[dict]:
    folder = job_directory(output_dir, job)
    exports = list((folder / "data").glob("*FullProcessedDataWithLabels.csv"))
    if len(exports) != 1:
        raise ValueError("Expected one processed, labelled export for the V2 project.")
    export = exports[0]
    if export.is_symlink() or not export.resolve().is_relative_to(output_dir.resolve()):
        raise ValueError("V2 processed export must be inside this run's output directory.")
    overview_columns = set(SUMMARY_COLUMNS.values()) | {"ifcproject_pl", "mfid_key", "rtid_key", "poid_key", "smeid_key"}
    frame = pd.read_csv(export, encoding="utf-8-sig", low_memory=False,
                        usecols=lambda column: column in overview_columns)
    required = {"project", "SubmissionDate", "phase_pl"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError("V2 export is missing overview fields: " + ", ".join(sorted(missing)))
    if frame.empty:
        raise ValueError("V2 processed export contains no records; previous snapshot preserved.")
    if not frame["project"].eq(job["project_name"]).all():
        raise ValueError("V2 export contains records from a different project.")
    frame["project_ref_pl"] = frame.get("ifcproject_pl", pd.Series(index=frame.index, dtype=object)).fillna(job["project_name"])
    frame["project_label_pl"] = job["project_name"]
    frame["entity_type_eng_pl"] = ENTITY_TYPES[job["type"]]
    for key in ("mfid_key", "rtid_key", "poid_key", "smeid_key"):
        if key in frame:
            frame["id_key"] = frame[key]
            break
    summaries = []
    for phase, group in frame.groupby("phase_pl", dropna=False):
        rows, _ = build_snapshot_dataframe(group)
        phase_key = "" if pd.isna(phase) else str(phase)
        for row in rows:
            row.update(pipeline_version="V2", source_key=source_key(job),
                       instance_key=json.dumps([source_key(job), phase_key], ensure_ascii=False),
                       project_key=project_key(job), source_survey=job["survey_name"],
                       data_folder_url=folder_url)
        summaries.extend(rows)
    return summaries


def collect_snapshots(manifest: dict, output_dir: Path) -> tuple[list, list, list, list]:
    """Only successful jobs replace their old data; failed jobs keep theirs."""
    uploaded = {item.get("relative_path"): item for item in manifest.get("sharepoint", {}).get("files", [])}
    summaries, files, refreshed, errors = [], [], [], []
    for entry in manifest.get("jobs", []):
        job = entry["job"]
        name = f"{job['project_name']} / {job['survey_name']}"
        if is_empty_job(entry):
            continue
        if not entry.get("success") or any(not item.get("Result") for item in entry.get("result", {}).get("Data", [])):
            errors.append(f"{name}: processing failed; previous snapshot preserved.")
            continue
        try:
            folder = job_directory(output_dir, job)
            job_files = []
            for path in sorted(folder.rglob("*")):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(output_dir.resolve()):
                    continue
                relative = path.relative_to(output_dir).as_posix()
                if any(part.startswith(".") for part in PurePosixPath(relative).parts):
                    continue
                item = uploaded.get(relative, {})
                inside = path.relative_to(folder).parts
                job_files.append({
                    "file_name": path.name, "local_path": str(path), "relative_path": relative,
                    "sharepoint_path": item.get("sharepoint_path", ""),
                    "status": item.get("status", "skipped"), "uploaded_at": item.get("uploaded_at"),
                    "web_url": item.get("web_url"), "folder_web_url": item.get("folder_web_url"),
                    "message": item.get("message") or manifest.get("sharepoint", {}).get("message", ""),
                    "source_key": source_key(job), "project_key": project_key(job),
                    "is_project_data": inside[0] == "data",
                })
            data_url = next((item["folder_web_url"] for item in job_files
                             if item["is_project_data"] and item["folder_web_url"]), None)
            rows = build_job_snapshot(output_dir, job, data_url)
            summaries.extend(rows)
            files.extend(job_files)
            refreshed.append(source_key(job))
        except (ValueError, OSError, KeyError) as exc:
            errors.append(f"{name}: {exc}")
    return summaries, files, refreshed, errors


def is_empty_job(entry: dict) -> bool:
    result = entry.get("result") or {}
    return (entry.get("success") is True and result.get("status") == "skipped"
            and result.get("reason") == "no_project_records")


def run_pipeline_and_snapshot(db_path: Path, *, run_id: int | None = None,
                              upload_to_sharepoint: bool = True,
                              triggered_by_email: str | None = None,
                              triggered_by_name: str | None = None, **_: Any) -> dict:
    db_path = Path(db_path).resolve()
    root = pipeline_root("V2")
    before = get_pipeline_repo_status()
    now = lambda: datetime.now(timezone.utc).isoformat()
    if run_id is None:
        run_id = insert_pipeline_run(db_path, status="running", extract_mode="configured",
                                     pipeline_version="V2", started_at=now(), triggered_by_email=triggered_by_email,
                                     triggered_by_name=triggered_by_name, pipeline_branch=before.get("branch"),
                                     pipeline_commit_before=before.get("commit"), message="V2 pipeline started.")
    with RUN_LOCK:
        log_path = None
        try:
            if not (root / "main.py").is_file():
                raise FileNotFoundError(f"V2 main.py was not found in {root}.")
            run_dir = root / "runs" / f"portal-{run_id}-{uuid4().hex[:8]}"
            run_dir.mkdir(parents=True)
            output_dir = run_dir / "output"
            manifest_path = run_dir / "manifest.json"
            log_path = run_dir / "run.log"
            env = os.environ.copy()
            # The legacy runner still loads its master ../.env itself. A copied
            # V2 checkout may instead hold SurveyCTO credentials in its own .env.
            for values in (dotenv_values(root / ".env"), dotenv_values(root.parent / ".env")):
                for key in ("SURVEYCTO_SERVER", "SURVEYCTO_USERNAME", "SURVEYCTO_PASSWORD"):
                    if values.get(key):
                        env[key] = values[key]
            command = [python_executable(root), "-u", str(root / "main.py"),
                       "--output", str(output_dir), "--manifest", str(manifest_path)]
            if not upload_to_sharepoint:
                command.append("--skip-sharepoint")
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=int(os.getenv("ALP_V2_TIMEOUT_SECONDS", "3600")), check=False)
            if not manifest_path.is_file():
                raise RuntimeError(f"V2 exited with code {result.returncode} without a run manifest. See the run log.")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            empty_jobs = [entry["job"] for entry in manifest.get("jobs", []) if is_empty_job(entry)]
            summaries, uploads, refreshed, errors = collect_snapshots(manifest, output_dir)
            if refreshed:
                publish_run_snapshot(db_path, run_id=run_id, pipeline_version="V2", survey_rows=summaries,
                                     record_rows=[], upload_rows=uploads, source_keys=refreshed)
            if result.returncode and not errors:
                errors.append(f"V2 runner exited with code {result.returncode}.")
            if manifest.get("sharepoint", {}).get("status") == "failed":
                errors.append("Some SharePoint uploads failed; see the file upload status.")
            if not refreshed and not errors and not empty_jobs:
                errors.append("V2 did not produce any project snapshots.")
            status = "partial" if errors and refreshed else "failed" if errors else "completed"
            if status == "completed" and upload_to_sharepoint:
                from ..auto_refresh import record_data_update
                record_data_update(db_path, run_id, "V2", uploads)
            message = f"V2 updated {len(summaries)} survey snapshots ({sum(r['submission_count'] for r in summaries)} processed records)."
            for job in empty_jobs:
                message += (f" {job['project_name']} / {job['survey_name']}: skipped because the configured source"
                            " has no matching project records; any existing snapshot kept.")
            if manifest.get("sharepoint", {}).get("status") == "skipped":
                message += " SharePoint upload skipped: " + manifest["sharepoint"].get("message", "not configured")
            if errors:
                message += " " + " ".join(errors)
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            complete_pipeline_run(db_path, run_id=run_id, status=status, completed_at=now(), message=message,
                                  pipeline_commit_after=get_pipeline_repo_status().get("commit"), run_log=log_text)
            return {"run_id": run_id, "status": status, "pipeline_version": "V2", "message": message,
                    "uploads": uploads, "log": log_text}
        except Exception as exc:
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path and log_path.exists() else str(exc)
            complete_pipeline_run(db_path, run_id=run_id, status="failed", completed_at=now(), message=str(exc),
                                  pipeline_commit_after=get_pipeline_repo_status().get("commit"), run_log=log_text)
            raise
