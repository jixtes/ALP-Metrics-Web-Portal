from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def connect_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


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
                selected_at TEXT NOT NULL
            );
            """
        )
        _ensure_column(connection, "survey_summaries", "blr", "INTEGER")
        _ensure_column(connection, "pipeline_runs", "triggered_by_email", "TEXT")
        _ensure_column(connection, "pipeline_runs", "triggered_by_name", "TEXT")
        _ensure_column(connection, "pipeline_runs", "pipeline_branch", "TEXT")
        _ensure_column(connection, "pipeline_runs", "pipeline_commit_before", "TEXT")
        _ensure_column(connection, "pipeline_runs", "pipeline_commit_after", "TEXT")
        _ensure_column(connection, "pipeline_runs", "run_log", "TEXT")
        connection.execute("DELETE FROM survey_records")
        connection.commit()


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
) -> int:
    with connect_database(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO pipeline_runs (
                status, extract_mode, started_at, triggered_by_email, triggered_by_name,
                pipeline_branch, pipeline_commit_before, message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        connection.commit()


def replace_run_snapshot(
    db_path: Path,
    *,
    run_id: int,
    survey_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
) -> None:
    with connect_database(db_path) as connection:
        connection.execute("DELETE FROM survey_summaries WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM survey_records WHERE run_id = ?", (run_id,))

        connection.executemany(
            """
            INSERT INTO survey_summaries (
                run_id, survey_name, project_ref, client, country, phase, cohort, assessor,
                trc, fpa, blr, submission_count, first_submission_at, last_submission_at, preview_json
            ) VALUES (
                :run_id, :survey_name, :project_ref, :client, :country, :phase, :cohort, :assessor,
                :trc, :fpa, :blr, :submission_count, :first_submission_at, :last_submission_at, :preview_json
            )
            """,
            [
                {
                    **row,
                    "run_id": run_id,
                    "preview_json": json.dumps(row["preview"], default=str),
                }
                for row in survey_rows
            ],
        )

        connection.executemany(
            """
            INSERT INTO survey_records (
                run_id, survey_name, submission_key, submission_date, enumerator, respondent_name,
                country, entity_type, target_group, raw_preview_json
            ) VALUES (
                :run_id, :survey_name, :submission_key, :submission_date, :enumerator, :respondent_name,
                :country, :entity_type, :target_group, :raw_preview_json
            )
            """,
            [
                {
                    **row,
                    "run_id": run_id,
                    "raw_preview_json": json.dumps(row["preview"], default=str),
                }
                for row in record_rows
            ],
        )
        connection.commit()


def replace_run_uploads(
    db_path: Path,
    *,
    run_id: int,
    upload_rows: list[dict[str, Any]],
) -> None:
    with connect_database(db_path) as connection:
        connection.execute("DELETE FROM pipeline_uploads WHERE run_id = ?", (run_id,))
        connection.executemany(
            """
            INSERT INTO pipeline_uploads (
                run_id, file_name, local_path, sharepoint_path, status,
                uploaded_at, web_url, message
            ) VALUES (
                :run_id, :file_name, :local_path, :sharepoint_path, :status,
                :uploaded_at, :web_url, :message
            )
            """,
            [{**row, "run_id": run_id} for row in upload_rows],
        )
        connection.commit()


def fetch_dashboard(db_path: Path) -> dict[str, Any]:
    with connect_database(db_path) as connection:
        latest_run = connection.execute(
            """
            SELECT id, status, extract_mode, started_at, triggered_by_email, triggered_by_name,
                   completed_at, message, pipeline_branch,
                   pipeline_commit_before, pipeline_commit_after, run_log
            FROM pipeline_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        summary_rows = connection.execute(
            """
            SELECT id, survey_name, project_ref, client, country, phase, cohort, assessor,
                   trc, fpa, blr, submission_count, first_submission_at, last_submission_at, preview_json
            FROM survey_summaries
            WHERE run_id = (
                SELECT id FROM pipeline_runs
                WHERE status = 'completed'
                ORDER BY id DESC
                LIMIT 1
            )
            ORDER BY submission_count DESC, survey_name ASC
            """
        ).fetchall()

        upload_rows = connection.execute(
            """
            SELECT id, file_name, local_path, sharepoint_path, status, uploaded_at, web_url, message
            FROM pipeline_uploads
            WHERE run_id = (
                SELECT id FROM pipeline_runs
                ORDER BY id DESC
                LIMIT 1
            )
            ORDER BY id ASC
            """
        ).fetchall()

        return {
            "latest_run": _decode_row(latest_run),
            "surveys": [_decode_summary(row) for row in summary_rows],
            "uploads": [_decode_row(row) for row in upload_rows],
        }


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
            SELECT id, report_id, report_name, dataset_id, embed_url, display_order, selected_at
            FROM powerbi_report_selections
            ORDER BY display_order ASC, id ASC
            """
        ).fetchall()
        return [_decode_row(row) for row in rows]


def replace_powerbi_report_selections(db_path: Path, selections: list[dict[str, Any]]) -> None:
    with connect_database(db_path) as connection:
        connection.execute("DELETE FROM powerbi_report_selections")
        connection.executemany(
            """
            INSERT INTO powerbi_report_selections (
                report_id, report_name, dataset_id, embed_url, display_order, selected_at
            ) VALUES (
                :report_id, :report_name, :dataset_id, :embed_url, :display_order, :selected_at
            )
            """,
            selections,
        )
        connection.commit()


def _decode_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


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
