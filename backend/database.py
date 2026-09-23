from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from collections.abc import Iterator
import sqlite3
from pathlib import Path
from typing import Any


@contextmanager
def connect_database(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(db_path: Path) -> None:
    with connect_database(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                extract_mode TEXT NOT NULL,
                started_at TEXT NOT NULL,
                triggered_by_email TEXT,
                triggered_by_name TEXT,
                completed_at TEXT,
                row_count INTEGER,
                survey_count INTEGER,
                message TEXT,
                pipeline_branch TEXT,
                pipeline_commit_before TEXT,
                pipeline_commit_after TEXT,
                run_log TEXT
            );

            CREATE TABLE IF NOT EXISTS survey_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                survey_name TEXT NOT NULL,
                project_ref TEXT,
                project_label TEXT,
                client TEXT,
                country TEXT,
                phase TEXT,
                cohort TEXT,
                assessor TEXT,
                trc INTEGER,
                fpa INTEGER,
                blr INTEGER,
                submission_count INTEGER NOT NULL,
                first_submission_at TEXT,
                last_submission_at TEXT,
                preview_json TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES pipeline_runs (id)
            );

            CREATE TABLE IF NOT EXISTS survey_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                survey_name TEXT NOT NULL,
                submission_key TEXT,
                submission_date TEXT,
                enumerator TEXT,
                respondent_name TEXT,
                country TEXT,
                entity_type TEXT,
                target_group TEXT,
                raw_preview_json TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES pipeline_runs (id)
            );

            CREATE TABLE IF NOT EXISTS pipeline_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                local_path TEXT NOT NULL,
                sharepoint_path TEXT,
                status TEXT NOT NULL,
                uploaded_at TEXT,
                web_url TEXT,
                message TEXT,
                FOREIGN KEY (run_id) REFERENCES pipeline_runs (id)
            );

            CREATE TABLE IF NOT EXISTS powerbi_report_selections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT NOT NULL UNIQUE,
                report_name TEXT NOT NULL,
                dataset_id TEXT,
                embed_url TEXT,
                display_order INTEGER NOT NULL,
                selected_at TEXT NOT NULL,
                project_scope TEXT NOT NULL DEFAULT 'all',
                allowed_project_refs_json TEXT NOT NULL DEFAULT '[]'
            );
            """
        )
        _ensure_column(connection, "survey_summaries", "blr", "INTEGER")
        _ensure_column(connection, "survey_summaries", "project_label", "TEXT")
        _ensure_column(connection, "pipeline_runs", "triggered_by_email", "TEXT")
        _ensure_column(connection, "pipeline_runs", "triggered_by_name", "TEXT")
        _ensure_column(connection, "pipeline_runs", "pipeline_branch", "TEXT")
        _ensure_column(connection, "pipeline_runs", "pipeline_commit_before", "TEXT")
        _ensure_column(connection, "pipeline_runs", "pipeline_commit_after", "TEXT")
        _ensure_column(connection, "pipeline_runs", "run_log", "TEXT")
        _ensure_column(connection, "powerbi_report_selections", "project_scope", "TEXT NOT NULL DEFAULT 'all'")
        _ensure_column(connection, "powerbi_report_selections", "allowed_project_refs_json", "TEXT NOT NULL DEFAULT '[]'")
        for table in ("pipeline_runs", "survey_summaries", "survey_records", "pipeline_uploads"):
            _ensure_column(connection, table, "pipeline_version", "TEXT NOT NULL DEFAULT 'V3'")
        for table in ("survey_summaries", "pipeline_uploads"):
            for name in ("source_key", "project_key"):
                _ensure_column(connection, table, name, "TEXT NOT NULL DEFAULT ''")
        for name in ("instance_key", "source_survey", "data_folder_url"):
            _ensure_column(connection, "survey_summaries", name, "TEXT")
        for name in ("relative_path", "folder_web_url"):
            _ensure_column(connection, "pipeline_uploads", name, "TEXT")
        _ensure_column(connection, "pipeline_uploads", "is_project_data", "INTEGER NOT NULL DEFAULT 0")
        connection.execute("UPDATE survey_summaries SET project_key = survey_name, source_key = survey_name, instance_key = survey_name WHERE pipeline_version = 'V3' AND project_key = ''")
        connection.execute("DELETE FROM survey_records")
        connection.commit()

    from .auto_refresh import initialize
    initialize(db_path)


class PipelineAlreadyRunning(Exception):
    def __init__(self, run_id: int):
        self.run_id = run_id
        super().__init__("A data update is already in progress. Wait for it to finish.")


def insert_pipeline_run(
    db_path: Path,
    *,
    status: str,
    extract_mode: str,
    started_at: str,
    triggered_by_email: str | None,
    triggered_by_name: str | None,
    pipeline_branch: str | None = None,
    pipeline_commit_before: str | None = None,
    message: str | None = None,
    pipeline_version: str = "V3",
    reject_if_running: bool = False,
) -> int:
    with connect_database(db_path) as connection:
        if reject_if_running:
            # Reserve the update across versions and concurrent web workers.
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute("""
                SELECT id FROM pipeline_runs
                WHERE status = 'running' AND id IN (
                    SELECT MAX(id) FROM pipeline_runs
                    WHERE extract_mode != 'surveycto_test'
                    GROUP BY pipeline_version
                ) LIMIT 1
            """).fetchone()
            if active:
                raise PipelineAlreadyRunning(active["id"])
            refresh = connection.execute("SELECT run_id FROM powerbi_refresh_jobs WHERE status NOT IN ('completed','failed','skipped') LIMIT 1").fetchone()
            if refresh:
                raise PipelineAlreadyRunning(refresh["run_id"])
        cursor = connection.execute(
            """
            INSERT INTO pipeline_runs (
                status, extract_mode, started_at, triggered_by_email, triggered_by_name,
                pipeline_branch, pipeline_commit_before, message, pipeline_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                status,
                extract_mode,
                started_at,
                triggered_by_email,
                triggered_by_name,
                pipeline_branch,
                pipeline_commit_before,
                message,
                pipeline_version,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def complete_pipeline_run(
    db_path: Path,
    *,
    run_id: int,
    status: str,
    completed_at: str,
    message: str | None,
    pipeline_commit_after: str | None = None,
    run_log: str | None = None,
) -> None:
    with connect_database(db_path) as connection:
        connection.execute(
            """
            UPDATE pipeline_runs
            SET status = ?, completed_at = ?, message = ?, pipeline_commit_after = ?, run_log = ?
            WHERE id = ?
            """,
            (status, completed_at, message, pipeline_commit_after, run_log, run_id),
        )
        connection.execute(
            "UPDATE pipeline_runs SET row_count = (SELECT COALESCE(SUM(submission_count), 0) FROM survey_summaries WHERE run_id = ?), survey_count = (SELECT COUNT(*) FROM survey_summaries WHERE run_id = ?) WHERE id = ?",
            (run_id, run_id, run_id),
        )
        connection.commit()


def _delete_snapshot_scope(connection, table: str, version: str, source_keys: list[str] | None) -> None:
    if version not in {"V2", "V3"}:
        raise ValueError("Unknown pipeline version.")
    if source_keys is None:
        connection.execute(f"DELETE FROM {table} WHERE pipeline_version = ?", (version,))
    elif source_keys:
        placeholders = ",".join("?" for _ in source_keys)
        connection.execute(f"DELETE FROM {table} WHERE pipeline_version = ? AND source_key IN ({placeholders})",
                           (version, *source_keys))


def replace_run_snapshot(db_path: Path, *, run_id: int, survey_rows: list[dict[str, Any]],
                         record_rows: list[dict[str, Any]], pipeline_version: str = "V3",
                         source_keys: list[str] | None = None, connection=None) -> None:
    own_connection = connection is None
    with connect_database(db_path) if own_connection else nullcontext(connection) as conn:
        _delete_snapshot_scope(conn, "survey_summaries", pipeline_version, source_keys)
        # Only aggregate summaries are retained; respondent previews remain disabled.
        conn.execute("DELETE FROM survey_records WHERE pipeline_version = ?", (pipeline_version,))
        conn.executemany(
            """
            INSERT INTO survey_summaries (
                run_id, pipeline_version, source_key, instance_key, project_key, source_survey, data_folder_url,
                survey_name, project_ref, project_label, client, country, phase, cohort, assessor,
                trc, fpa, blr, submission_count, first_submission_at, last_submission_at, preview_json
            ) VALUES (
                :run_id, :pipeline_version, :source_key, :instance_key, :project_key, :source_survey, :data_folder_url,
                :survey_name, :project_ref, :project_label, :client, :country, :phase, :cohort, :assessor,
                :trc, :fpa, :blr, :submission_count, :first_submission_at, :last_submission_at, :preview_json
            )
            """,
            [{**row, "run_id": run_id, "pipeline_version": pipeline_version,
              "source_key": row.get("source_key", row["survey_name"]),
              "instance_key": row.get("instance_key", row["survey_name"]),
              "project_key": row.get("project_key", row["survey_name"]),
              "source_survey": row.get("source_survey"), "data_folder_url": row.get("data_folder_url"),
              "preview_json": json.dumps(row["preview"], default=str)} for row in survey_rows],
        )
        if own_connection:
            conn.commit()


def replace_run_uploads(db_path: Path, *, run_id: int, upload_rows: list[dict[str, Any]],
                       pipeline_version: str = "V3", source_keys: list[str] | None = None,
                       connection=None) -> None:
    own_connection = connection is None
    with connect_database(db_path) if own_connection else nullcontext(connection) as conn:
        _delete_snapshot_scope(conn, "pipeline_uploads", pipeline_version, source_keys)
        conn.executemany(
            """
            INSERT INTO pipeline_uploads (
                run_id, pipeline_version, source_key, project_key, relative_path, folder_web_url, is_project_data,
                file_name, local_path, sharepoint_path, status, uploaded_at, web_url, message
            ) VALUES (
                :run_id, :pipeline_version, :source_key, :project_key, :relative_path, :folder_web_url, :is_project_data,
                :file_name, :local_path, :sharepoint_path, :status, :uploaded_at, :web_url, :message
            )
            """,
            [{**row, "run_id": run_id, "pipeline_version": pipeline_version,
              "source_key": row.get("source_key", ""), "project_key": row.get("project_key", ""),
              "relative_path": row.get("relative_path"), "folder_web_url": row.get("folder_web_url"),
              "is_project_data": int(bool(row.get("is_project_data"))), "message": row.get("message")}
             for row in upload_rows],
        )
        if own_connection:
            conn.commit()


def publish_run_snapshot(db_path: Path, *, run_id: int, pipeline_version: str,
                         survey_rows: list[dict[str, Any]], record_rows: list[dict[str, Any]],
                         upload_rows: list[dict[str, Any]], source_keys: list[str] | None = None) -> None:
    """Replace summaries and files together, preserving other pipeline sources."""
    with connect_database(db_path) as connection:
        replace_run_snapshot(db_path, run_id=run_id, pipeline_version=pipeline_version,
                             survey_rows=survey_rows, record_rows=record_rows,
                             source_keys=source_keys, connection=connection)
        replace_run_uploads(db_path, run_id=run_id, pipeline_version=pipeline_version,
                           upload_rows=upload_rows, source_keys=source_keys, connection=connection)
        connection.commit()


def fetch_dashboard(db_path: Path) -> dict[str, Any]:
    with connect_database(db_path) as connection:
        latest_run = connection.execute(
            """
            SELECT *
            FROM pipeline_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        summary_rows = connection.execute(
            """
            SELECT *
            FROM survey_summaries
            ORDER BY submission_count DESC, survey_name ASC
            """
        ).fetchall()

        upload_rows = connection.execute(
            """
            SELECT *
            FROM pipeline_uploads
            ORDER BY id ASC
            """
        ).fetchall()

        latest_runs = {version: _decode_row(connection.execute(
            "SELECT * FROM pipeline_runs WHERE pipeline_version = ? AND extract_mode != 'surveycto_test' ORDER BY id DESC LIMIT 1",
            (version,),
        ).fetchone()) for version in ("V2", "V3")}
        return {
            "latest_runs": latest_runs,
            "latest_run": _decode_row(latest_run),
            "surveys": [_decode_summary(row) for row in summary_rows],
            "uploads": [_decode_row(row) for row in upload_rows],
        }


def fetch_pipeline_run(db_path: Path, run_id: int) -> dict[str, Any] | None:
    with connect_database(db_path) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM pipeline_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        return _decode_row(row)


def fetch_survey_records(db_path: Path, survey_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with connect_database(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, survey_name, submission_key, submission_date, enumerator, respondent_name,
                   country, entity_type, target_group, raw_preview_json
            FROM survey_records
            WHERE run_id = (
                SELECT run_id FROM survey_summaries WHERE id = ?
            )
            AND survey_name = (
                SELECT survey_name FROM survey_summaries WHERE id = ?
            )
            ORDER BY submission_date DESC, id DESC
            LIMIT ?
            """,
            (survey_id, survey_id, limit),
        ).fetchall()
        return [_decode_record(row) for row in rows]


def fetch_powerbi_report_selections(db_path: Path) -> list[dict[str, Any]]:
    with connect_database(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, report_id, report_name, dataset_id, embed_url, display_order, selected_at,
                   project_scope, allowed_project_refs_json
            FROM powerbi_report_selections
            ORDER BY display_order ASC, id ASC
            """
        ).fetchall()
        return [_decode_powerbi_selection(row) for row in rows]


def replace_powerbi_report_selections(db_path: Path, selections: list[dict[str, Any]]) -> None:
    with connect_database(db_path) as connection:
        connection.execute("DELETE FROM powerbi_report_selections")
        connection.executemany(
            """
            INSERT INTO powerbi_report_selections (
                report_id, report_name, dataset_id, embed_url, display_order, selected_at,
                project_scope, allowed_project_refs_json
            ) VALUES (
                :report_id, :report_name, :dataset_id, :embed_url, :display_order, :selected_at,
                :project_scope, :allowed_project_refs_json
            )
            """,
            [
                {
                    **selection,
                    "project_scope": selection.get("project_scope") or "all",
                    "allowed_project_refs_json": json.dumps(selection.get("allowed_project_refs") or []),
                }
                for selection in selections
            ],
        )
        connection.commit()


def _decode_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _decode_powerbi_selection(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        allowed_project_refs = json.loads(item.pop("allowed_project_refs_json") or "[]")
    except json.JSONDecodeError:
        allowed_project_refs = []
    item["allowed_project_refs"] = [
        str(project_ref).strip()
        for project_ref in allowed_project_refs
        if str(project_ref).strip()
    ] if isinstance(allowed_project_refs, list) else []
    item["project_scope"] = item.get("project_scope") or "all"
    return item


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    columns = {row["name"] for row in rows}
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _decode_summary(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["preview"] = json.loads(item.pop("preview_json"))
    return item


def _decode_record(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["preview"] = json.loads(item.pop("raw_preview_json"))
    return item
