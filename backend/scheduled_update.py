"""Production timer entry point for V3; shares the portal's database reservation."""
from datetime import datetime, timedelta, timezone
import fcntl
from pathlib import Path

from .database import (
    PipelineAlreadyRunning, PipelineRecentlyStarted, complete_pipeline_run, connect_database,
    fetch_pipeline_run, initialize_database, insert_pipeline_run,
)
from .service import get_pipeline_repo_status, run_pipeline_and_snapshot

SCHEDULE_ACTOR = "Automatic schedule"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _recover_interrupted_runs(db_path):
    # The caller owns the scheduler file lock, so no older scheduled process can
    # still be running. Never change manual runs or the durable Power BI jobs.
    with connect_database(db_path) as conn:
        conn.execute("""UPDATE pipeline_runs SET status='failed', completed_at=?, message=?
            WHERE status='running' AND pipeline_version='V3' AND extract_mode='surveycto'
            AND triggered_by_name=? AND triggered_by_email IS NULL""",
            (_now(), "Scheduled update was interrupted before completion.", SCHEDULE_ACTOR))


def run_scheduled_update(db_path: Path) -> int:
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise ValueError(f"Portal database not found: {db_path}")
    with open(str(db_path) + ".scheduled-update.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Skipped: a scheduled update is already running.", flush=True)
            return 0
        initialize_database(db_path)
        _recover_interrupted_runs(db_path)
        repo = get_pipeline_repo_status("V3")
        started_at = datetime.now(timezone.utc)
        try:
            run_id = insert_pipeline_run(
                db_path, status="running", reject_if_running=True,
                extract_mode="surveycto", pipeline_version="V3", started_at=started_at.isoformat(),
                skip_if_started_since=(started_at - timedelta(hours=1)).isoformat(),
                triggered_by_email=None, triggered_by_name=SCHEDULE_ACTOR,
                pipeline_branch=repo.get("branch"), pipeline_commit_before=repo.get("commit"),
                message="Scheduled V3 data update started.",
            )
        except PipelineRecentlyStarted:
            print("Skipped: V3 was already triggered within the last hour.", flush=True)
            return 0
        except PipelineAlreadyRunning:
            print("Skipped: a data update or dashboard refresh is active. Next attempt is at the next scheduled time.", flush=True)
            return 0
        print(f"Started scheduled V3 update, portal run {run_id}.", flush=True)
        try:
            result = run_pipeline_and_snapshot(
                db_path, run_id=run_id, pipeline_version="V3", extract_mode="surveycto",
                upload_to_sharepoint=True, publish_snapshot=True,
                triggered_by_email=None, triggered_by_name=SCHEDULE_ACTOR,
            )
        except BaseException:
            # Also cover service termination/timeouts before an adapter can record
            # failure. Its more detailed error, when already recorded, is preserved.
            run = fetch_pipeline_run(db_path, run_id)
            if run and run["status"] == "running":
                complete_pipeline_run(db_path, run_id=run_id, status="failed", completed_at=_now(),
                                      message="Scheduled V3 update stopped before completion.")
            print(f"Scheduled V3 update failed or was interrupted; see portal run {run_id}.", flush=True)
            return 1
        run = fetch_pipeline_run(db_path, run_id)
        if run["status"] == "running":
            complete_pipeline_run(db_path, run_id=run_id, status="failed", completed_at=_now(),
                                  message="Scheduled V3 update returned without completing its run.")
            print(f"Scheduled V3 update did not complete; see portal run {run_id}.", flush=True)
            return 1
        incomplete_uploads = any(item.get("status") != "uploaded" for item in result.get("uploads", []))
        if run["status"] != "completed" or incomplete_uploads:
            print(f"Scheduled V3 update finished with errors; see portal run {run_id}.", flush=True)
            return 1
        print(f"Completed scheduled V3 update, portal run {run_id}. Eligible Power BI refreshes are handled by the portal worker.", flush=True)
        return 0
