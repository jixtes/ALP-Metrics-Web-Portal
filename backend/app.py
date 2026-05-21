from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from flask_security import auth_required, current_user, roles_required

from powerbi.client import PowerBIClient, PowerBIConfig

from .auth import current_user_project_access, current_user_report_access, current_user_upload_access, init_auth
from .database import (
    fetch_dashboard,
    fetch_powerbi_report_selections,
    initialize_database,
    replace_powerbi_report_selections,
)
from .service import (
    APP_DB_PATH,
    get_pipeline_commit_details,
    get_pipeline_repo_status,
    pull_pipeline_repo,
    run_pipeline_and_snapshot,
)


def _current_powerbi_identity() -> tuple[str | None, list[str] | None]:
    username = getattr(current_user, "email", None)
    roles = sorted({role.name for role in getattr(current_user, "roles", []) if getattr(role, "name", None)})
    return username, roles or None


def create_app() -> Flask:
    root_dir = Path(__file__).resolve().parents[1]
    frontend_dist = root_dir / "frontend" / "dist"
    instance_dir = root_dir / "instance"
    app = Flask(
        __name__,
        static_folder=None,
        instance_path=str(instance_dir),
    )
    init_auth(app)

    db_path = Path(app.config.get("DATABASE_PATH", APP_DB_PATH))
    initialize_database(db_path)

    @app.get("/api/health")
    def healthcheck():
        return jsonify({"status": "ok"})

    @app.get("/api/dashboard")
    @auth_required("session")
    def dashboard():
        dashboard_data = fetch_dashboard(db_path)
        dashboard_data["settings"] = {
            "resetLinkHours": int(app.config["ALP_RESET_LINK_HOURS"]),
        }
        project_scope, allowed_project_refs = current_user_project_access()
        if project_scope == "restricted":
            dashboard_data["surveys"] = [
                survey for survey in dashboard_data["surveys"] if (survey.get("project_ref") or "") in allowed_project_refs
            ]
        if not current_user_upload_access():
            dashboard_data["uploads"] = []
        latest_run = dashboard_data.get("latest_run")
        if latest_run and latest_run.get("pipeline_commit_after"):
            latest_run.update(get_pipeline_commit_details(latest_run.get("pipeline_commit_after")))
        return jsonify(dashboard_data)

    @app.get("/api/surveys/<int:survey_id>/records")
    @auth_required("session")
    def survey_records(survey_id: int):
        return jsonify({"error": "Record-level survey previews are disabled for data privacy."}), 410

    @app.post("/api/pipeline/run")
    @auth_required("session")
    def run_pipeline():
        payload = request.get_json(silent=True) or {}
        extract_mode = str(payload.get("extractMode", "surveycto")).strip().lower() or "surveycto"
        if extract_mode not in {"surveycto", "csv"}:
            return jsonify({"error": "extractMode must be either 'surveycto' or 'csv'."}), 400

        try:
            result = run_pipeline_and_snapshot(
                db_path,
                extract_mode=extract_mode,
                upload_to_sharepoint=True,
                triggered_by_email=getattr(current_user, "email", None),
                triggered_by_name=getattr(current_user, "full_name", None),
            )
            return jsonify(result), 201
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/pipeline/status")
    @auth_required("session")
    @roles_required("admin")
    def pipeline_status():
        try:
            return jsonify(get_pipeline_repo_status())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/pipeline/pull")
    @auth_required("session")
    @roles_required("admin")
    def pipeline_pull():
        try:
            result = pull_pipeline_repo()
            status_code = 200 if result["status"] in {"completed", "blocked"} else 500
            return jsonify(result), status_code
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

    @app.get("/api/powerbi/reports")
    @auth_required("session")
    @roles_required("admin")
    def powerbi_reports():
        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            reports = client.list_reports()
            dataset_cache: dict[str, dict] = {}

            def dataset_metadata(dataset_id: str | None) -> dict:
                if not dataset_id:
                    return {}
                if dataset_id not in dataset_cache:
                    try:
                        dataset_cache[dataset_id] = client.get_dataset(dataset_id)
                    except Exception:
                        dataset_cache[dataset_id] = {}
                return dataset_cache[dataset_id]

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
        report_scope, allowed_report_ids = current_user_report_access()
        if report_scope == "restricted":
            selections = [report for report in selections if report.get("report_id") in allowed_report_ids]
        return jsonify({"reports": selections})

    @app.put("/api/powerbi/selections")
    @auth_required("session")
    @roles_required("admin")
    def update_powerbi_selections():
        payload = request.get_json(silent=True) or {}
        report_ids = payload.get("reportIds")
        if not isinstance(report_ids, list) or not all(isinstance(item, str) and item.strip() for item in report_ids):
            return jsonify({"error": "reportIds must be a list of report ID strings."}), 400

        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            reports = client.list_reports()
            report_lookup = {str(report.get("id")): report for report in reports if report.get("id")}
            missing = [report_id for report_id in report_ids if report_id not in report_lookup]
            if missing:
                return jsonify({"error": f"Unknown report IDs: {', '.join(missing)}"}), 400

            timestamp = datetime.now(tz=timezone.utc).isoformat()
            selections = [
                {
                    "report_id": report_id,
                    "report_name": report_lookup[report_id].get("name") or report_id,
                    "dataset_id": report_lookup[report_id].get("datasetId"),
                    "embed_url": report_lookup[report_id].get("embedUrl"),
                    "display_order": index,
                    "selected_at": timestamp,
                }
                for index, report_id in enumerate(report_ids)
            ]
            replace_powerbi_report_selections(db_path, selections)
            return jsonify({"reports": fetch_powerbi_report_selections(db_path)})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/powerbi/embed-configs")
    @auth_required("session")
    def powerbi_embed_configs():
        try:
            config = PowerBIConfig.from_env()
            client = PowerBIClient(config)
            selections = fetch_powerbi_report_selections(db_path)
            report_scope, allowed_report_ids = current_user_report_access()
            if report_scope == "restricted":
                selections = [report for report in selections if report.get("report_id") in allowed_report_ids]
            embed_configs = []
            for selection in selections:
                try:
                    username, roles = _current_powerbi_identity()
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
