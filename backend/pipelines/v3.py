from __future__ import annotations

import contextlib
import importlib
import io
import shutil
import subprocess
import sys
import traceback
import warnings
from pathlib import Path
from typing import Any

from .repository import RUN_LOCK, pipeline_root

PIPELINE_ROOT = pipeline_root("V3")
if PIPELINE_ROOT.exists() and str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from ..database import complete_pipeline_run, insert_pipeline_run, publish_run_snapshot
from .snapshots import build_snapshot_rows, _now_iso

WEB_PORTAL_ROOT = Path(__file__).resolve().parents[2]
APP_DB_PATH = WEB_PORTAL_ROOT / "instance" / "alp_metrics.db"
FINAL_EXPORT_PATH = Path("files/pipeline/alp_metrics_final_data.csv")
LABELLED_EXPORT_PATH = Path("files/pipeline/alp_metrics_final_data_with_labels.csv")
LATEST_PIPELINE_LOG_PATH = Path("files/pipeline/latest_run.log")
PIPELINE_MODULE_NAMES = {
    "config",
    "context",
    "pipeline",
}
PIPELINE_MODULE_PREFIXES = (
    "generated",
    "scripts",
    "utils",
    "optional_steps",
)



class PipelineExecutionError(Exception):
    def __init__(self, original: Exception, run_log: str) -> None:
        super().__init__(str(original))
        self.original = original
        self.run_log = run_log


def run_pipeline_and_snapshot(
    db_path: Path,
    *,
    run_id: int | None = None,
    extract_mode: str = "surveycto",
    upload_to_sharepoint: bool = False,
    publish_snapshot: bool = True,
    sharepoint_folder: str | None = None,
    triggered_by_email: str | None = None,
    triggered_by_name: str | None = None,
) -> dict[str, Any]:
    db_path = Path(db_path).resolve()
    started_at = _now_iso()
    pipeline_status = get_pipeline_repo_status()
    if run_id is None:
        run_id = insert_pipeline_run(
            db_path,
            status="running",
            extract_mode=extract_mode,
            started_at=started_at,
            triggered_by_email=triggered_by_email,
            triggered_by_name=triggered_by_name,
            pipeline_branch=pipeline_status.get("branch"),
            pipeline_commit_before=pipeline_status.get("commit"),
            message="Pipeline execution started.",
        )

    with RUN_LOCK:
        run_log = ""
        try:
            config = _build_pipeline_config(extract_mode=extract_mode)
            _write_latest_pipeline_log(config.root_dir, "Pipeline execution started.\n")
            run_log = _run_pipeline_with_log_capture(config)
            export_path = _resolve_export_path(config)
            survey_rows, record_rows = build_snapshot_rows(export_path)

            upload_rows = []
            if upload_to_sharepoint:
                upload_rows = _upload_export_files(
                    config.root_dir,
                    exports_dir=config.exports_dir,
                    sharepoint_folder=sharepoint_folder,
                )
            if publish_snapshot:
                publish_run_snapshot(db_path, run_id=run_id, pipeline_version="V3",
                                     survey_rows=survey_rows, record_rows=record_rows, upload_rows=upload_rows)

            uploaded_count = sum(1 for row in upload_rows if row["status"] == "uploaded")
            failed_count = sum(1 for row in upload_rows if row["status"] == "failed")
            skipped_count = sum(1 for row in upload_rows if row["status"] == "skipped")
            upload_message = (
                f" Uploads: {uploaded_count} uploaded, {failed_count} failed, {skipped_count} skipped."
                if upload_to_sharepoint
                else ""
            )

            complete_pipeline_run(
                db_path,
                run_id=run_id,
                status="completed",
                completed_at=_now_iso(),
                message=f"Pipeline completed.{upload_message}",
                pipeline_commit_after=get_pipeline_repo_status().get("commit"),
                run_log=run_log,
            )
            return {
                "run_id": run_id,
                "status": "completed",
                "export_path": str(export_path),
                "pipeline": get_pipeline_repo_status(),
                "log": run_log,
                "uploads": upload_rows,
            }
        except Exception as exc:
            if isinstance(exc, PipelineExecutionError):
                run_log = exc.run_log
                exc = exc.original
            if not run_log:
                run_log = str(exc)
            complete_pipeline_run(
                db_path,
                run_id=run_id,
                status="failed",
                completed_at=_now_iso(),
                message=str(exc),
                pipeline_commit_after=get_pipeline_repo_status().get("commit"),
                run_log=run_log,
            )
            raise


def get_pipeline_repo_status() -> dict[str, Any]:
    root_dir = PIPELINE_ROOT
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], root_dir)
    upstream = _git_output(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root_dir)
    commit = _git_output(["rev-parse", "HEAD"], root_dir)
    dirty_output = _git_output(["status", "--porcelain"], root_dir)
    return {
        "pipeline_version": "V3",
        "root": str(root_dir),
        "branch": branch,
        "upstream": upstream or (f"origin/{branch}" if branch else ""),
        "commit": commit,
        "isDirty": bool(dirty_output),
        "dirtyFiles": dirty_output.splitlines() if dirty_output else [],
    }


def get_pipeline_commit_details(commit: str | None) -> dict[str, str]:
    if not commit:
        return {}

    root_dir = PIPELINE_ROOT
    subject = _git_output(["show", "-s", "--format=%s", commit], root_dir)
    committed_at = _git_output(["show", "-s", "--format=%cI", commit], root_dir)
    author = _git_output(["show", "-s", "--format=%an", commit], root_dir)
    return {
        "pipeline_commit_subject": subject,
        "pipeline_commit_at": committed_at,
        "pipeline_commit_author": author,
    }


def pull_pipeline_repo() -> dict[str, Any]:
    root_dir = PIPELINE_ROOT
    before = get_pipeline_repo_status()
    if before["isDirty"]:
        return {
            "status": "blocked",
            "before": before,
            "after": before,
            "output": "Pipeline repository has local changes. Commit or clean them before pulling.",
        }

    with RUN_LOCK:
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", before["branch"]],
            cwd=root_dir,
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
    if result.returncode == 0:
        _clear_generated_pipeline_modules(root_dir)
        _reload_pipeline_imports()
    after = get_pipeline_repo_status()
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    return {
        "status": "completed" if result.returncode == 0 else "failed",
        "returnCode": result.returncode,
        "before": before,
        "after": after,
        "output": output,
    }


def _run_pipeline_with_log_capture(config: Any) -> str:
    stream = io.StringIO()
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            warnings.simplefilter("ignore")
            _run_pipeline(config)
    except Exception as exc:
        traceback.print_exc(file=stream)
        run_log = stream.getvalue().strip()
        _write_latest_pipeline_log(config.root_dir, run_log)
        raise PipelineExecutionError(exc, run_log) from exc

    run_log = stream.getvalue().strip()
    _write_latest_pipeline_log(config.root_dir, run_log)
    return run_log


def _build_pipeline_config(**kwargs: Any) -> Any:
    config_module = importlib.import_module("config")
    return config_module.PipelineConfig(**kwargs)


def _run_pipeline(config: Any) -> Any:
    pipeline_module = importlib.import_module("pipeline")
    return pipeline_module.run_pipeline(config)


def _upload_export_files(
    root_dir: Path,
    *,
    exports_dir: str,
    sharepoint_folder: str | None = None,
) -> list[dict[str, Any]]:
    sharepoint_module = importlib.import_module("scripts.sharepoint")
    if exports_dir == "files/pipeline" and sharepoint_folder is None:
        # Preserve the existing production behavior, which publishes the full files tree.
        return sharepoint_module.upload_export_files(root_dir)

    exports_root = root_dir / exports_dir
    export_paths = [path.relative_to(root_dir) for path in exports_root.rglob("*") if path.is_file()]
    return sharepoint_module.upload_export_files(
        root_dir,
        export_paths,
        target_folder=sharepoint_folder,
    )


def _clear_generated_pipeline_modules(root_dir: Path) -> None:
    generated_dir = root_dir / "generated"
    if generated_dir.exists():
        shutil.rmtree(generated_dir)


def _reload_pipeline_imports() -> None:
    importlib.invalidate_caches()
    for module_name in list(sys.modules):
        if module_name in PIPELINE_MODULE_NAMES or module_name.startswith(
            tuple(f"{prefix}." for prefix in PIPELINE_MODULE_PREFIXES)
        ) or module_name in PIPELINE_MODULE_PREFIXES:
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()


def _write_latest_pipeline_log(root_dir: Path, run_log: str) -> Path:
    log_path = root_dir / LATEST_PIPELINE_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(run_log.rstrip() + "\n", encoding="utf-8")
    return log_path


def _git_output(args: list[str], root_dir: Path) -> str:
    if not root_dir.is_dir():
        return ""
    result = subprocess.run(
        ["git", *args],
        cwd=root_dir,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _resolve_export_path(config: Any) -> Path:
    plain = config.root_dir / config.processed_csv_path
    labelled = config.root_dir / config.labeled_csv_path
    if plain.exists():
        return plain
    if labelled.exists():
        return labelled
    raise FileNotFoundError("No pipeline export file was found after the run.")
