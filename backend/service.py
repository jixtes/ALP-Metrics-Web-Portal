from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import pandas as pd

DEFAULT_PIPELINE_ROOT = Path(__file__).resolve().parents[2] / "alp-metrics-pipeline"
PIPELINE_ROOT = Path(os.getenv("ALP_PIPELINE_REPO_PATH", DEFAULT_PIPELINE_ROOT)).expanduser()
if PIPELINE_ROOT.exists() and str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from config import PipelineConfig
from pipeline import run_pipeline
from scripts.sharepoint import upload_export_files

from .database import complete_pipeline_run, insert_pipeline_run, replace_run_snapshot, replace_run_uploads

WEB_PORTAL_ROOT = Path(__file__).resolve().parents[1]
APP_DB_PATH = WEB_PORTAL_ROOT / "instance" / "alp_metrics.db"
FINAL_EXPORT_PATH = Path("files/pipeline/alp_metrics_final_data.csv")
LABELLED_EXPORT_PATH = Path("files/pipeline/alp_metrics_final_data_with_labels.csv")
LATEST_PIPELINE_LOG_PATH = Path("files/pipeline/latest_run.log")
RUN_LOCK = Lock()

SUMMARY_COLUMNS = {
    "survey_name": "project",
    "project_ref": "project_ref_pl",
    "client": "client_pl",
    "country": "country_pl",
    "phase": "phase_pl",
    "cohort": "cohort_pl",
    "assessor": "assessor_pl",
    "trc": "trc_pl",
    "fpa": "fpa_pl",
    "blr": "blr_pl",
    "submission_date": "SubmissionDate",
    "submission_key": "id_key",
    "enumerator": "enumerator",
    "respondent_name": "resp_name_pl",
    "entity_type": "entity_type_eng_pl",
    "target_group": "entity_target_group_eng_pl",
}

SURVEY_PREVIEW_FIELDS = [
    "survey_name",
    "project_ref",
    "client",
    "country",
    "phase",
    "cohort",
    "assessor",
    "trc",
    "fpa",
    "blr",
]

RECORD_PREVIEW_FIELDS = [
    "submission_key",
    "submission_date",
    "enumerator",
    "respondent_name",
    "country",
    "entity_type",
    "target_group",
]

RECENT_DAYS_LIMIT = 7
ENUMERATOR_DAILY_LIMIT = 50


class PipelineExecutionError(Exception):
    def __init__(self, original: Exception, run_log: str) -> None:
        super().__init__(str(original))
        self.original = original
        self.run_log = run_log


def run_pipeline_and_snapshot(
    db_path: Path,
    *,
    extract_mode: str = "surveycto",
    upload_to_sharepoint: bool = False,
    triggered_by_email: str | None = None,
    triggered_by_name: str | None = None,
) -> dict[str, Any]:
    db_path = Path(db_path).resolve()
    started_at = _now_iso()
    pipeline_status = get_pipeline_repo_status()
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
            config = PipelineConfig(extract_mode=extract_mode)
            _write_latest_pipeline_log(config.root_dir, "Pipeline execution started.\n")
            run_log = _run_pipeline_with_log_capture(config)
            export_path = _resolve_export_path(config.root_dir)
            survey_rows, record_rows = build_snapshot_rows(export_path)
            replace_run_snapshot(db_path, run_id=run_id, survey_rows=survey_rows, record_rows=record_rows)

            upload_rows = []
            if upload_to_sharepoint:
                upload_rows = upload_export_files(config.root_dir)
            replace_run_uploads(db_path, run_id=run_id, upload_rows=upload_rows)

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
    root_dir = PipelineConfig().root_dir
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], root_dir)
    upstream = _git_output(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root_dir)
    commit = _git_output(["rev-parse", "HEAD"], root_dir)
    dirty_output = _git_output(["status", "--porcelain"], root_dir)
    return {
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

    root_dir = PipelineConfig().root_dir
    subject = _git_output(["show", "-s", "--format=%s", commit], root_dir)
    committed_at = _git_output(["show", "-s", "--format=%cI", commit], root_dir)
    author = _git_output(["show", "-s", "--format=%an", commit], root_dir)
    return {
        "pipeline_commit_subject": subject,
        "pipeline_commit_at": committed_at,
        "pipeline_commit_author": author,
    }


def pull_pipeline_repo() -> dict[str, Any]:
    root_dir = PipelineConfig().root_dir
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
    after = get_pipeline_repo_status()
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    return {
        "status": "completed" if result.returncode == 0 else "failed",
        "returnCode": result.returncode,
        "before": before,
        "after": after,
        "output": output,
    }


def _run_pipeline_with_log_capture(config: PipelineConfig) -> str:
    stream = io.StringIO()
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            warnings.simplefilter("ignore")
            run_pipeline(config)
    except Exception as exc:
        traceback.print_exc(file=stream)
        run_log = stream.getvalue().strip()
        _write_latest_pipeline_log(config.root_dir, run_log)
        raise PipelineExecutionError(exc, run_log) from exc

    run_log = stream.getvalue().strip()
    _write_latest_pipeline_log(config.root_dir, run_log)
    return run_log


def _write_latest_pipeline_log(root_dir: Path, run_log: str) -> Path:
    log_path = root_dir / LATEST_PIPELINE_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(run_log.rstrip() + "\n", encoding="utf-8")
    return log_path


def _git_output(args: list[str], root_dir: Path) -> str:
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


def build_snapshot_rows(export_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dataframe = pd.read_csv(export_path, encoding="utf-8-sig")
    normalized = _normalize_dataframe(dataframe)
    if normalized.empty:
        return [], []

    normalized["submission_date"] = pd.to_datetime(normalized["submission_date"], errors="coerce")
    survey_rows: list[dict[str, Any]] = []
    record_rows: list[dict[str, Any]] = []

    grouped = normalized.groupby("survey_name", dropna=False)
    for survey_name, group in grouped:
        non_null_group = group.copy()
        non_null_group = non_null_group.sort_values("submission_date", ascending=False, na_position="last")

        survey_rows.append(
            {
                "survey_name": _stringify(survey_name),
                "project_ref": _first_non_null(group["project_ref"]),
                "client": _first_non_null(group["client"]),
                "country": _first_non_null(group["country"]),
                "phase": _first_non_null(group["phase"]),
                "cohort": _first_non_null(group["cohort"]),
                "assessor": _first_non_null(group["assessor"]),
                "trc": _int_or_none(_first_non_null(group["trc"])),
                "fpa": _int_or_none(_first_non_null(group["fpa"])),
                "blr": _int_or_none(_first_non_null(group["blr"])),
                "submission_count": int(len(group.index)),
                "first_submission_at": _datetime_to_iso(group["submission_date"].min()),
                "last_submission_at": _datetime_to_iso(group["submission_date"].max()),
                "preview": {
                    field: _preview_value(non_null_group.iloc[0][field]) if field in non_null_group.columns else None
                    for field in SURVEY_PREVIEW_FIELDS
                }
                | {
                    "daily_submission_counts": _daily_submission_counts(group),
                    "entity_daily_counts": _entity_daily_counts(group),
                    "enumerator_daily_counts": _enumerator_daily_counts(group),
                    "entity_category_counts": _entity_category_counts(group),
                    "active_enumerator_count": _active_enumerator_count(group["enumerator"]),
                    "entity_type_count": _entity_type_count(group["entity_type"]),
                    "entity_type_totals": _value_totals(group["entity_type"]),
                    "most_entity_types": _most_common_values(group["entity_type"]),
                    "most_target_groups": _most_common_values(group["target_group"]),
                },
            }
        )

    survey_rows.sort(key=lambda item: (-item["submission_count"], item["survey_name"]))
    return survey_rows, record_rows


def _resolve_export_path(root_dir: Path) -> Path:
    plain = root_dir / FINAL_EXPORT_PATH
    labelled = root_dir / LABELLED_EXPORT_PATH
    if plain.exists():
        return plain
    if labelled.exists():
        return labelled
    raise FileNotFoundError("No pipeline export file was found after the run.")


def _normalize_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
    renamed = dataframe.rename(columns={source: target for target, source in SUMMARY_COLUMNS.items() if source in dataframe.columns})
    required_columns = list(SUMMARY_COLUMNS.keys())
    for column in required_columns:
        if column not in renamed.columns:
            renamed[column] = None
    normalized = renamed[required_columns].copy()
    normalized["country"] = normalized["country"].map(_normalize_country)
    return normalized


def _normalize_country(value: Any) -> Any:
    text = _stringify(value)
    if text == "15":
        return "Nigeria"
    return value


def _first_non_null(series: pd.Series) -> str | None:
    for value in series:
        if pd.notna(value) and str(value).strip():
            return str(value).strip()
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _stringify(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _preview_value(value: Any) -> str | int | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if float(value).is_integer() else float(value)
    return str(value).strip() or None


def _most_common_values(series: pd.Series, limit: int = 3) -> list[str]:
    cleaned = series.map(_stringify).dropna()
    if cleaned.empty:
        return []

    counts = cleaned.value_counts()
    return [f"{value} ({int(count)})" for value, count in counts.head(limit).items()]


def _value_totals(series: pd.Series) -> list[str]:
    cleaned = series.map(_stringify).dropna()
    if cleaned.empty:
        return []

    counts = cleaned.value_counts()
    return [f"{value} ({int(count)})" for value, count in counts.items() if int(count) > 0]


def _daily_submission_counts(group: pd.DataFrame, limit: int = RECENT_DAYS_LIMIT) -> list[dict[str, Any]]:
    dated = group["submission_date"].dropna()
    if dated.empty:
        return []

    counts = dated.dt.date.value_counts().sort_index(ascending=False).head(limit)
    return [{"date": date.isoformat(), "count": int(count)} for date, count in counts.items()]


def _enumerator_daily_counts(group: pd.DataFrame, limit: int = ENUMERATOR_DAILY_LIMIT) -> list[dict[str, Any]]:
    dated = group[["submission_date", "enumerator"]].dropna(subset=["submission_date"]).copy()
    if dated.empty:
        return []

    recent_dates = dated["submission_date"].dt.date.drop_duplicates().sort_values(ascending=False).head(RECENT_DAYS_LIMIT)
    dated["date"] = dated["submission_date"].dt.date
    dated["enumerator"] = dated["enumerator"].map(_stringify).fillna("Unknown enumerator")
    dated = dated[dated["date"].isin(set(recent_dates))]
    counts = (
        dated.groupby(["date", "enumerator"], dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["date", "count", "enumerator"], ascending=[False, False, True])
        .head(limit)
    )

    return [
        {"date": row["date"].isoformat(), "enumerator": row["enumerator"], "count": int(row["count"])}
        for _, row in counts.iterrows()
    ]


def _entity_daily_counts(group: pd.DataFrame, limit: int = ENUMERATOR_DAILY_LIMIT) -> list[dict[str, Any]]:
    dated = group[["submission_date", "entity_type"]].dropna(subset=["submission_date"]).copy()
    if dated.empty:
        return []

    recent_dates = dated["submission_date"].dt.date.drop_duplicates().sort_values(ascending=False).head(RECENT_DAYS_LIMIT)
    dated["date"] = dated["submission_date"].dt.date
    dated["entity_type"] = dated["entity_type"].map(_stringify).fillna("Unknown entity type")
    dated = dated[dated["date"].isin(set(recent_dates))]
    counts = (
        dated.groupby(["date", "entity_type"], dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["date", "count", "entity_type"], ascending=[False, False, True])
        .head(limit)
    )

    return [
        {"date": row["date"].isoformat(), "entity_type": row["entity_type"], "count": int(row["count"])}
        for _, row in counts.iterrows()
    ]


def _entity_category_counts(group: pd.DataFrame) -> dict[str, int]:
    counts = {"pos": 0, "retailers": 0, "lead_farmers": 0}
    for _, row in group.iterrows():
        category = _entity_category_key(row.get("entity_type"), row.get("target_group"))
        if category:
            counts[category] += 1
    return counts


def _entity_category_key(entity_type: Any, target_group: Any = None) -> str | None:
    values = [_stringify(entity_type), _stringify(target_group)]
    haystack = " ".join(value.lower() for value in values if value)
    entity = (values[0] or "").lower()

    if entity == "po" or "producer organization" in haystack or "producer organisation" in haystack:
        return "pos"
    if entity == "rt" or "retail" in haystack:
        return "retailers"
    if entity == "lf" or "lead farmer" in haystack:
        return "lead_farmers"
    return None


def _active_enumerator_count(series: pd.Series) -> int:
    cleaned = series.map(_stringify).dropna()
    return int(cleaned.nunique())


def _entity_type_count(series: pd.Series) -> int:
    cleaned = series.map(_stringify).dropna()
    return int(cleaned.nunique())


def _datetime_to_iso(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        return timestamp.isoformat()
    return timestamp.tz_convert(timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
