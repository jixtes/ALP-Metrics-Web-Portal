from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
from threading import Thread

from flask import Flask, jsonify, request, send_from_directory
from flask_security import auth_required, current_user, roles_required

from powerbi.client import PowerBIClient, PowerBIConfig

from .auth import (
    current_user_project_access,
    current_user_upload_access,
    init_auth,
    user_access_preview,
)
from .database import (
    fetch_dashboard,
    fetch_pipeline_run,
    fetch_powerbi_report_selections,
    insert_pipeline_run,
    complete_pipeline_run,
    PipelineAlreadyRunning,
    initialize_database,
    replace_powerbi_report_selections,
)
from .service import (
    APP_DB_PATH,
    get_pipeline_commit_details,
    get_pipeline_repo_status,
    pull_pipeline_repo,
    run_pipeline_and_snapshot,
    normalize_pipeline_version,
)
from .survey_relay import register_survey_relay_routes
from .surveycto_webhook import register_surveycto_webhook_route
from . import auto_refresh


INDIVIDUAL_REPORT_NAME = os.getenv("INDIVIDUAL_REPORT_NAME", "IR_PO_Baseline_v3_test").strip()
INDIVIDUAL_REPORT_RLS_ROLES = ["IR Web Demo User RLS"]


def _current_powerbi_identity(preview: dict | None = None) -> tuple[str | None, list[str] | None]:
    username = getattr(current_user, "email", None)
    if preview:
        username = preview.get("user", {}).get("email") or username
        return username, [preview["role"]["name"]]
    roles = sorted({role.name for role in getattr(current_user, "roles", []) if getattr(role, "name", None)})
    return username, roles or None


def _run_pipeline_background(
    db_path: Path,
    *,
    run_id: int,
    extract_mode: str,
    pipeline_version: str = "V3",
    triggered_by_email: str | None,
    triggered_by_name: str | None,
) -> None:
    try:
        run_pipeline_and_snapshot(
            db_path,
            run_id=run_id,
            extract_mode=extract_mode,
            pipeline_version=pipeline_version,
            upload_to_sharepoint=True,
            triggered_by_email=triggered_by_email,
            triggered_by_name=triggered_by_name,
        )
    except Exception:
        # run_pipeline_and_snapshot records failed runs in the database.
        pass


def create_app(config: dict | None = None) -> Flask:
    root_dir = Path(__file__).resolve().parents[1]
    frontend_dist = root_dir / "frontend" / "dist"
    instance_dir = root_dir / "instance"
    app = Flask(
        __name__,
        static_folder=None,
        instance_path=str(instance_dir),
    )
    if config:
        app.config.update(config)
    init_auth(app)
    register_survey_relay_routes(app)

    db_path = Path(app.config.get("DATABASE_PATH", APP_DB_PATH))
    initialize_database(db_path)
    register_surveycto_webhook_route(app, db_path)
    if not app.config.get("TESTING"):
        auto_refresh.start_worker(db_path)

        @app.before_request
        def ensure_refresh_worker():
            auto_refresh.start_worker(db_path)

    @app.get("/api/powerbi/auto-refresh")
    @auth_required("session")
    @roles_required("admin")
    def get_auto_refresh_settings():
        return jsonify(auto_refresh.settings(db_path))

    @app.put("/api/powerbi/auto-refresh")
    @auth_required("session")
    @roles_required("admin")
    def put_auto_refresh_settings():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected automatic refresh settings."}), 400
        try:
            return jsonify(auto_refresh.save_settings(db_path, payload))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            return jsonify({"error": "Could not verify Fabric capacity or Power BI access. Check the resource ID and app permissions."}), 502

    @app.get("/api/powerbi/auto-refresh/status")
    @auth_required("session")
    def get_auto_refresh_status():
        preview, preview_error = _user_preview_from_request()
        if preview_error:
            return jsonify({"error": preview_error}), 403
        job = auto_refresh.latest_job(db_path)
        if job and (preview or not current_user.has_role("admin")):
            selections = _filter_powerbi_selections_for_access(fetch_powerbi_report_selections(db_path), preview)
            allowed_datasets = {item.get("dataset_id") for item in selections}
            job["datasets"] = [item for item in job["datasets"] if item["datasetId"] in allowed_datasets]
            job.pop("dashboards", None)
            job.pop("error", None)
            job["message"] = {
                "queued": "Dashboard refresh queued.", "scaling_up": "Scaling capacity to F16.",
                "refreshing": "Refreshing dashboards.", "restoring": "Restoring capacity to F2.",
                "completed": "Dashboards refreshed; capacity restored to F2.",
                "skipped": "No changed data; dashboard refresh skipped.",
                "failed": "Automatic dashboard refresh failed. Check with an administrator.",
            }.get(job["status"], "Dashboard refresh in progress.")
        return jsonify({"job": job})

    @app.get("/api/health")
    def healthcheck():
        return jsonify({"status": "ok"})

    @app.get("/api/dashboard")
    @auth_required("session")
    def dashboard():
        preview, preview_error = _user_preview_from_request()
        if preview_error:
            return jsonify({"error": preview_error}), 403
        dashboard_data = fetch_dashboard(db_path)
        dashboard_data["uploads"] = _hide_test_survey_uploads(dashboard_data.get("uploads", []))
        dashboard_data["settings"] = {
            "resetLinkHours": int(app.config["ALP_RESET_LINK_HOURS"]),
        }
        project_scope, allowed_project_refs = _project_access(preview)
        if project_scope == "restricted":
            dashboard_data["surveys"] = [
                survey for survey in dashboard_data["surveys"] if _survey_project_access_key(survey) in allowed_project_refs
            ]
        upload_scope = _upload_access(preview)
        if upload_scope == "none":
            dashboard_data["uploads"] = []
        elif upload_scope == "project_files":
            dashboard_data["uploads"] = _filter_project_file_uploads(
                dashboard_data["uploads"],
                dashboard_data["surveys"],
                project_scope,
                allowed_project_refs,
            )
        if upload_scope == "none":
            for survey in dashboard_data["surveys"]:
                survey["data_folder_url"] = None
        latest_run = dashboard_data.get("latest_run")
        if latest_run and latest_run.get("pipeline_commit_after"):
            latest_run.update(get_pipeline_commit_details(latest_run.get("pipeline_commit_after"), latest_run["pipeline_version"]))
        for version, run in dashboard_data["latest_runs"].items():
            if run and run.get("pipeline_commit_after"):
                run.update(get_pipeline_commit_details(run["pipeline_commit_after"], version))
        # Legacy V2 logs can contain printed response rows. Only administrators
        # may inspect logs; project-level access exposes aggregate snapshots.
        if preview or not current_user.has_role("admin"):
            for run in [latest_run, *dashboard_data["latest_runs"].values()]:
                if run:
                    run.pop("run_log", None)
        if preview:
            dashboard_data["accessPreview"] = {
                "role": preview["role"],
                "user": preview.get("user"),
            }
        return jsonify(dashboard_data)

    @app.get("/api/surveys/<int:survey_id>/records")
    @auth_required("session")
    def survey_records(survey_id: int):
        return jsonify({"error": "Record-level survey previews are disabled for data privacy."}), 410

    @app.post("/api/pipeline/run")
    @auth_required("session")
    def run_pipeline():
        payload = request.get_json(silent=True) or {}
        try:
            version = normalize_pipeline_version(payload.get("pipelineVersion", "V3"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        extract_mode = "surveycto" if version == "V3" else "configured"
        pipeline_status = get_pipeline_repo_status(version)
        try:
            run_id = insert_pipeline_run(
                db_path,
                status="running",
                reject_if_running=True,
                extract_mode=extract_mode,
                pipeline_version=version,
                started_at=datetime.now(tz=timezone.utc).isoformat(),
                triggered_by_email=getattr(current_user, "email", None),
                triggered_by_name=getattr(current_user, "full_name", None),
                pipeline_branch=pipeline_status.get("branch"),
                pipeline_commit_before=pipeline_status.get("commit"),
                message=f"{version} pipeline execution started.",
            )
        except PipelineAlreadyRunning as exc:
            return jsonify({"error": str(exc), "run_id": exc.run_id}), 409

        thread = Thread(
            target=_run_pipeline_background,
            kwargs={
                "db_path": db_path,
                "run_id": run_id,
                "extract_mode": extract_mode,
                "pipeline_version": version,
                "triggered_by_email": getattr(current_user, "email", None),
                "triggered_by_name": getattr(current_user, "full_name", None),
            },
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            complete_pipeline_run(db_path, run_id=run_id, status="failed",
                                  completed_at=datetime.now(tz=timezone.utc).isoformat(),
                                  message="Unable to start the data update.")
            raise
        return jsonify({"run_id": run_id, "status": "running", "pipeline_version": version,
                        "message": f"{version} pipeline execution started."}), 202

    @app.get("/api/pipeline/runs/<int:run_id>")
    @auth_required("session")
    def pipeline_run(run_id: int):
        run = fetch_pipeline_run(db_path, run_id)
        if run is None:
            return jsonify({"error": "Pipeline run not found."}), 404
        if not current_user.has_role("admin"):
            run.pop("run_log", None)
        return jsonify(run)

    @app.get("/api/pipeline/status")
    @auth_required("session")
    @roles_required("admin")
    def pipeline_status():
        try:
            version = normalize_pipeline_version(request.args.get("pipelineVersion", "V3"))
            return jsonify(get_pipeline_repo_status(version))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/pipeline/pull")
    @auth_required("session")
    @roles_required("admin")
    def pipeline_pull():
        try:
            payload = request.get_json(silent=True) or {}
            version = normalize_pipeline_version(payload.get("pipelineVersion", "V3"))
            if version != "V3":
                return jsonify({"error": "Pipeline code updates from Settings are available for V3 only."}), 400
            result = pull_pipeline_repo(version)
            status_code = 200 if result["status"] in {"completed", "blocked"} else 500
            return jsonify(result), status_code
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/powerbi/embed-config")
    @auth_required("session")
    def powerbi_embed_config():
        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            username, roles = _current_powerbi_identity()
            return jsonify(client.build_embed_config(username=username, roles=roles))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/powerbi/individual-report")
    @auth_required("session")
    def individual_powerbi_report():
        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            reports = client.list_reports()
            report = next((item for item in reports if item.get("name") == INDIVIDUAL_REPORT_NAME), None)
            if report is None:
                return jsonify({"error": f"Power BI report '{INDIVIDUAL_REPORT_NAME}' was not found."}), 404

            embed_config = client.build_embed_config(
                report_id=report["id"],
                dataset_id=report.get("datasetId"),
                username=str(current_user.email).strip().lower(),
                roles=INDIVIDUAL_REPORT_RLS_ROLES,
            )
            try:
                history = client.get_refresh_history(report.get("datasetId"), top=1)
                latest_refresh = history[0] if history else None
            except Exception:
                latest_refresh = None
            return jsonify({**embed_config, "latestRefresh": latest_refresh, "error": None})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/powerbi/reports")
    @auth_required("session")
    @roles_required("admin")
    def powerbi_reports():
        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            reports = client.list_reports()
            _prune_powerbi_selections(db_path, reports)
            dataset_cache: dict[str, dict] = {}
            refresh_cache: dict[str, dict | None] = {}

            def dataset_metadata(dataset_id: str | None) -> dict:
                if not dataset_id:
                    return {}
                if dataset_id not in dataset_cache:
                    try:
                        dataset_cache[dataset_id] = client.get_dataset(dataset_id)
                    except Exception:
                        dataset_cache[dataset_id] = {}
                return dataset_cache[dataset_id]

            def latest_refresh(dataset_id: str | None) -> dict | None:
                if not dataset_id:
                    return None
                if dataset_id not in refresh_cache:
                    try:
                        history = client.get_refresh_history(dataset_id, top=1)
                        refresh_cache[dataset_id] = history[0] if history else None
                    except Exception:
                        refresh_cache[dataset_id] = None
                return refresh_cache[dataset_id]

            return jsonify(
                {
                    "reports": [
                        {
                            "id": report.get("id"),
                            "name": report.get("name"),
                            "datasetId": report.get("datasetId"),
                            "embedUrl": report.get("embedUrl"),
                            "webUrl": report.get("webUrl"),
                            "isEffectiveIdentityRequired": dataset_metadata(report.get("datasetId")).get(
                                "isEffectiveIdentityRequired", False
                            ),
                            "isEffectiveIdentityRolesRequired": dataset_metadata(report.get("datasetId")).get(
                                "isEffectiveIdentityRolesRequired", False
                            ),
                            "latestRefresh": latest_refresh(report.get("datasetId")),
                        }
                        for report in reports
                    ]
                }
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/powerbi/selections")
    @auth_required("session")
    def powerbi_selections():
        selections = fetch_powerbi_report_selections(db_path)
        preview, preview_error = _user_preview_from_request()
        if preview_error:
            return jsonify({"error": preview_error}), 403
        selections = _filter_powerbi_selections_for_access(selections, preview)
        return jsonify({"reports": selections})

    @app.put("/api/powerbi/selections")
    @auth_required("session")
    @roles_required("admin")
    def update_powerbi_selections():
        payload = request.get_json(silent=True) or {}
        report_payloads = payload.get("reports")
        if isinstance(report_payloads, list):
            requested_reports = report_payloads
        else:
            report_ids = payload.get("reportIds")
            if not isinstance(report_ids, list):
                return jsonify({"error": "reports must be a list."}), 400
            requested_reports = [{"reportId": report_id} for report_id in report_ids]

        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            reports = client.list_reports()
            report_lookup = {str(report.get("id")): report for report in reports if report.get("id")}
            valid_report_payloads = []
            for report_payload in requested_reports:
                if not isinstance(report_payload, dict):
                    continue
                report_id = str(report_payload.get("reportId") or report_payload.get("report_id") or "").strip()
                if report_id in report_lookup:
                    valid_report_payloads.append(report_payload)

            timestamp = datetime.now(tz=timezone.utc).isoformat()
            selections = [
                {
                    "report_id": report_id,
                    "report_name": report_lookup[report_id].get("name") or report_id,
                    "dataset_id": report_lookup[report_id].get("datasetId"),
                    "embed_url": report_lookup[report_id].get("embedUrl"),
                    "display_order": index,
                    "selected_at": timestamp,
                    "project_scope": _powerbi_project_scope(report_payload),
                    "allowed_project_refs": _clean_string_list(report_payload.get("allowedProjectRefs")),
                }
                for index, report_payload in enumerate(valid_report_payloads)
                for report_id in [str(report_payload.get("reportId") or report_payload.get("report_id") or "").strip()]
            ]
            replace_powerbi_report_selections(db_path, selections)
            return jsonify({"reports": fetch_powerbi_report_selections(db_path)})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/powerbi/refresh")
    @auth_required("session")
    @roles_required("admin")
    def refresh_powerbi_dataset():
        if auto_refresh.active_job(db_path):
            return jsonify({"error": "Wait for the automatic dashboard refresh and F2 restoration to finish."}), 409
        payload = request.get_json(silent=True) or {}
        dataset_id = str(payload.get("datasetId", "")).strip() or None

        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            if not dataset_id:
                selections = fetch_powerbi_report_selections(db_path)
                dataset_ids = [selection["dataset_id"] for selection in selections if selection.get("dataset_id")]
                unique_dataset_ids = sorted(set(dataset_ids))
                if len(unique_dataset_ids) == 1:
                    dataset_id = unique_dataset_ids[0]
                elif len(unique_dataset_ids) > 1:
                    return jsonify({"error": "Select one report to refresh because multiple semantic models are shown."}), 400
            result = client.refresh_dataset(dataset_id=dataset_id)
            return jsonify({"message": "Power BI semantic model refresh started.", **result}), 202
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/powerbi/embed-configs")
    @auth_required("session")
    def powerbi_embed_configs():
        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            selections = fetch_powerbi_report_selections(db_path)
            preview, preview_error = _user_preview_from_request()
            if preview_error:
                return jsonify({"error": preview_error}), 403
            selections = _filter_powerbi_selections_for_access(selections, preview)
            embed_configs = []
            refresh_cache: dict[str, dict | None] = {}

            def latest_refresh(dataset_id: str | None) -> dict | None:
                if not dataset_id:
                    return None
                if dataset_id not in refresh_cache:
                    try:
                        history = client.get_refresh_history(dataset_id, top=1)
                        refresh_cache[dataset_id] = history[0] if history else None
                    except Exception:
                        refresh_cache[dataset_id] = None
                return refresh_cache[dataset_id]

            for selection in selections:
                try:
                    username, roles = _current_powerbi_identity(preview)
                    embed_config = client.build_embed_config(
                        report_id=selection["report_id"],
                        dataset_id=selection["dataset_id"],
                        username=username,
                        roles=roles,
                    )
                    embed_configs.append(
                        {
                            **embed_config,
                            "selectionId": selection["id"],
                            "selectedAt": selection["selected_at"],
                            "latestRefresh": latest_refresh(embed_config.get("datasetId")),
                            "error": None,
                        }
                    )
                except Exception as exc:
                    embed_configs.append(
                        {
                            "type": "report",
                            "reportId": selection["report_id"],
                            "reportName": selection["report_name"],
                            "datasetId": selection["dataset_id"],
                            "embedUrl": selection["embed_url"],
                            "accessToken": None,
                            "tokenExpiration": None,
                            "selectionId": selection["id"],
                            "selectedAt": selection["selected_at"],
                            "latestRefresh": latest_refresh(selection.get("dataset_id")),
                            "error": str(exc),
                        }
                    )
            return jsonify({"reports": embed_configs})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/", defaults={"path": ""})
    @app.get("/<path:path>")
    def serve_frontend(path: str):
        if path.startswith("api/"):
            return jsonify({"error": "Not found"}), 404
        if frontend_dist.exists():
            asset = frontend_dist / path
            if path and asset.exists():
                return send_from_directory(frontend_dist, path)
            return send_from_directory(frontend_dist, "index.html")
        return jsonify({"message": "Frontend build not found. Run the React app in frontend/."})

    return app


PROJECT_DATA_UPLOAD_FOLDER = "pipeline/project_data"
HIDDEN_TEST_UPLOAD_FOLDERS = {"local_update", "test_survey"}
PROJECT_FILE_SUFFIXES = (
    "_final_data_with_labels.csv",
    "_final_data.csv",
    ".csv",
)


def _hide_test_survey_uploads(uploads: list[dict]) -> list[dict]:
    return [upload for upload in uploads if not _is_test_survey_upload(upload)]


def _is_test_survey_upload(upload: dict) -> bool:
    for field_name in ("local_path", "sharepoint_path"):
        path_parts = {
            part.casefold()
            for part in str(upload.get(field_name) or "").replace("\\", "/").split("/")
            if part
        }
        if path_parts.intersection(HIDDEN_TEST_UPLOAD_FOLDERS):
            return True
    return False


def _filter_project_file_uploads(
    uploads: list[dict],
    surveys: list[dict],
    project_scope: str,
    allowed_project_refs: set[str],
) -> list[dict]:
    project_data_uploads = [upload for upload in uploads if _is_project_data_upload(upload)]
    if project_scope != "restricted":
        return project_data_uploads

    allowed_survey_names = {
        str(survey.get("survey_name") or "")
        for survey in surveys
        if survey.get("pipeline_version", "V3") == "V3" and _survey_project_access_key(survey) in allowed_project_refs
    }
    allowed_project_file_slugs = {_slugify_project_file_name(name) for name in allowed_survey_names if name}
    return [
        upload
        for upload in project_data_uploads
        if (upload.get("pipeline_version") == "V2" and upload.get("project_key") in allowed_project_refs)
        or (upload.get("pipeline_version", "V3") == "V3"
            and _project_file_slug(upload.get("file_name") or _upload_relative_path(upload)) in allowed_project_file_slugs)
    ]


def _user_preview_from_request() -> tuple[dict | None, str | None]:
    if "userPreviewId" not in request.args:
        return None, None
    try:
        user_id = int(request.args.get("userPreviewId", ""))
    except ValueError:
        return None, "Invalid user preview."
    preview = user_access_preview(user_id)
    if not preview:
        return None, "User preview is only available to admins for non-admin users."
    return preview, None


def _project_access(preview: dict | None) -> tuple[str, set[str]]:
    if preview:
        return preview["project_scope"], preview["allowed_project_refs"]
    return current_user_project_access()


def _upload_access(preview: dict | None) -> str:
    if preview:
        return preview["upload_scope"]
    return current_user_upload_access()


def _filter_powerbi_selections_for_access(selections: list[dict], preview: dict | None) -> list[dict]:
    project_scope, allowed_project_refs = _project_access(preview)
    if project_scope != "restricted":
        return selections

    return [
        selection
        for selection in selections
        if selection.get("project_scope") != "restricted"
        or allowed_project_refs.intersection(set(selection.get("allowed_project_refs") or []))
    ]


def _powerbi_project_scope(report_payload: dict) -> str:
    project_scope = str(report_payload.get("projectScope") or report_payload.get("project_scope") or "all").strip().lower()
    return "restricted" if project_scope == "restricted" else "all"


def _clean_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _prune_powerbi_selections(db_path, reports: list[dict]) -> None:
    current_report_ids = {str(report.get("id")) for report in reports if report.get("id")}
    saved_selections = fetch_powerbi_report_selections(db_path)
    if not saved_selections:
        return

    kept_selections = [
        {
            "report_id": selection["report_id"],
            "report_name": selection["report_name"],
            "dataset_id": selection.get("dataset_id"),
            "embed_url": selection.get("embed_url"),
            "display_order": index,
            "selected_at": selection["selected_at"],
            "project_scope": selection.get("project_scope") or "all",
            "allowed_project_refs": selection.get("allowed_project_refs") or [],
        }
        for index, selection in enumerate(saved_selections)
        if selection.get("report_id") in current_report_ids
    ]
    if len(kept_selections) != len(saved_selections):
        replace_powerbi_report_selections(db_path, kept_selections)


def _survey_project_access_key(survey: dict) -> str:
    return str(survey.get("project_key") or survey.get("survey_name") or "")


def _is_project_data_upload(upload: dict) -> bool:
    if upload.get("pipeline_version") == "V2":
        parts = str(upload.get("relative_path") or "").split("/")
        return bool(upload.get("is_project_data")) and len(parts) >= 4 and parts[2] == "data"
    relative_path = f"/{_upload_relative_path(upload).lower().strip('/')}/"
    return f"/{PROJECT_DATA_UPLOAD_FOLDER}/" in relative_path


def _project_file_slug(file_name: str) -> str:
    normalized_name = str(file_name).replace("\\", "/").rsplit("/", 1)[-1]
    lower_name = normalized_name.lower()
    for suffix in PROJECT_FILE_SUFFIXES:
        if lower_name.endswith(suffix):
            normalized_name = normalized_name[: -len(suffix)]
            break
    return _slugify_project_file_name(normalized_name)


def _slugify_project_file_name(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")


def _upload_relative_path(upload: dict) -> str:
    raw_path = str(upload.get("sharepoint_path") or upload.get("local_path") or "").replace("\\", "/")
    lower_path = raw_path.lower()
    for marker in ("root:/", "alp-metrics-pipeline/", "/files/"):
        marker_index = lower_path.find(marker)
        if marker_index >= 0:
            raw_path = raw_path[marker_index + len(marker) :]
            lower_path = raw_path.lower()
    return raw_path.strip("/")
