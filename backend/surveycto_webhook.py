from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
from pathlib import Path
from threading import Lock, Thread
import time
from typing import Any

from flask import Flask, jsonify, request

from powerbi.client import PowerBIClient, PowerBIConfig

from .auth import csrf, ensure_individual_report_account
from .email_service import send_report_ready_email, send_report_ready_setup_email
from .service import run_pipeline_and_snapshot


DEFAULT_TEST_SHAREPOINT_FOLDER = "ALP Metrics/Exports/local_update"
DEFAULT_INDIVIDUAL_REPORT_NAME = "IR_PO_Baseline_v3_test"
DEFAULT_RESPONDENT_NAME_PREFIX = "Jigsa Bulto"
_state_lock = Lock()
_run_active = False
_pending_jobs: list[WebhookJob] = []
_recently_queued: dict[str, float] = {}


@dataclass(frozen=True)
class WebhookJob:
    recipient: str
    portal_origin: str

    @property
    def report_url(self) -> str:
        return f"{self.portal_origin.rstrip('/')}/individual-report"


def _webhook_secret() -> str:
    return os.getenv("SURVEYCTO_WEBHOOK_SECRET", "").strip()


def _authorized(token: str) -> bool:
    secret = _webhook_secret()
    return bool(secret) and hmac.compare_digest(token, secret)


def _request_value(field_name: str) -> str:
    form_values = request.form.getlist(field_name)
    value: Any = form_values[-1] if form_values else None
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        value = payload.get(field_name, value)
        nested_data = payload.get("data")
        if value is None and isinstance(nested_data, dict):
            value = nested_data.get(field_name)
    if isinstance(value, list):
        value = value[-1] if value else ""
    return str(value or "").strip()


def _request_email() -> str:
    # request_user_email remains as a temporary compatibility fallback.
    return (_request_value("report_user_email") or _request_value("request_user_email")).lower()


def _request_respondent_name() -> str:
    return _request_value("resp_name_pl")


def _portal_origin() -> str:
    configured = os.getenv("PORTAL_PUBLIC_URL", "").strip().rstrip("/")
    return configured or request.url_root.rstrip("/")


def _test_sharepoint_folder() -> str:
    configured = os.getenv("SURVEYCTO_TEST_SHAREPOINT_FOLDER", "").strip().strip("/")
    return configured or DEFAULT_TEST_SHAREPOINT_FOLDER


def _history_key(refresh: dict[str, Any]) -> str:
    return str(refresh.get("requestId") or refresh.get("id") or refresh.get("startTime") or "")


def _refresh_individual_report() -> dict[str, Any]:
    client = PowerBIClient(PowerBIConfig.from_env())
    report_name = os.getenv("INDIVIDUAL_REPORT_NAME", DEFAULT_INDIVIDUAL_REPORT_NAME).strip()
    report = next((item for item in client.list_reports() if item.get("name") == report_name), None)
    if report is None:
        raise RuntimeError(f"Power BI report '{report_name}' was not found.")
    dataset_id = str(report.get("datasetId") or "").strip()
    if not dataset_id:
        raise RuntimeError(f"Power BI report '{report_name}' has no semantic model ID.")

    previous_history = client.get_refresh_history(dataset_id, top=10)
    previous_keys = {_history_key(item) for item in previous_history if _history_key(item)}
    refresh_request = client.refresh_dataset(dataset_id=dataset_id)
    request_id = str(refresh_request.get("requestId") or "").strip()
    timeout_seconds = max(30, int(os.getenv("POWERBI_REFRESH_TIMEOUT_SECONDS", "900")))
    poll_seconds = max(1, int(os.getenv("POWERBI_REFRESH_POLL_SECONDS", "5")))
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        history = client.get_refresh_history(dataset_id, top=10)
        target = None
        if request_id:
            target = next((item for item in history if str(item.get("requestId") or "") == request_id), None)
        if target is None:
            target = next((item for item in history if _history_key(item) not in previous_keys), None)
        if target is not None:
            status = str(target.get("status") or "").strip().lower()
            if status == "completed":
                return {"report": report_name, "dataset_id": dataset_id, "refresh": target}
            if status in {"failed", "cancelled", "disabled"}:
                raise RuntimeError(f"Power BI refresh ended with status '{target.get('status')}'.")
        time.sleep(poll_seconds)

    raise TimeoutError(f"Power BI refresh did not complete within {timeout_seconds} seconds.")


def _notify_job(job: WebhookJob) -> None:
    account = ensure_individual_report_account(job.recipient, reset_origin=job.portal_origin)
    if account["created"]:
        reset = account["reset"]
        result = send_report_ready_setup_email(
            recipient=job.recipient,
            reset_url=reset["reset_url"],
            expires_at=reset["expires_at"].isoformat(),
        )
    else:
        result = send_report_ready_email(recipient=job.recipient, report_url=job.report_url)
    if not result.sent:
        raise RuntimeError(f"Email to {job.recipient} was not sent: {result.error}")


def _run_test_pipeline(app: Flask, db_path: Path) -> None:
    global _run_active

    with app.app_context():
        while True:
            with _state_lock:
                if not _pending_jobs:
                    _run_active = False
                    return
                jobs_by_email = {job.recipient: job for job in _pending_jobs}
                _pending_jobs.clear()

            try:
                result = run_pipeline_and_snapshot(
                    db_path,
                    extract_mode="surveycto_test",
                    upload_to_sharepoint=True,
                    publish_snapshot=False,
                    sharepoint_folder=_test_sharepoint_folder(),
                    triggered_by_email="surveycto-webhook",
                    triggered_by_name="SurveyCTO test webhook",
                )
                uploads = result.get("uploads", [])
                unsuccessful = [item for item in uploads if item.get("status") != "uploaded"]
                if not uploads or unsuccessful:
                    raise RuntimeError(
                        f"SharePoint upload did not fully succeed: {len(uploads) - len(unsuccessful)} uploaded, "
                        f"{len(unsuccessful)} unsuccessful."
                    )
                _refresh_individual_report()
                for job in jobs_by_email.values():
                    try:
                        _notify_job(job)
                    except Exception as exc:
                        print(f"SurveyCTO notification failed: {exc}", flush=True)
            except Exception as exc:
                print(f"SurveyCTO test pipeline workflow failed: {exc}", flush=True)


def _queue_test_pipeline(app: Flask, db_path: Path, job: WebhookJob) -> str:
    global _run_active

    with _state_lock:
        now = time.monotonic()
        dedup_seconds = max(0, int(os.getenv("SURVEYCTO_WEBHOOK_DEDUP_SECONDS", "300")))
        previous_queue_time = _recently_queued.get(job.recipient)
        if previous_queue_time is not None and now - previous_queue_time < dedup_seconds:
            return "duplicate"
        _recently_queued[job.recipient] = now
        expired_recipients = [
            recipient
            for recipient, queued_at in _recently_queued.items()
            if now - queued_at >= dedup_seconds
        ]
        for recipient in expired_recipients:
            _recently_queued.pop(recipient, None)

        _pending_jobs.append(job)
        if _run_active:
            return "queued"
        _run_active = True

    Thread(
        target=_run_test_pipeline,
        args=(app, db_path),
        name="surveycto-test-pipeline",
        daemon=True,
    ).start()
    return "started"


def register_surveycto_webhook_route(app: Flask, db_path: Path) -> None:
    @app.post("/api/webhooks/surveycto")
    @csrf.exempt
    def surveycto_webhook():
        if not _authorized(request.args.get("token", "")):
            return jsonify({"error": "Invalid webhook token."}), 401

        respondent_name = _request_respondent_name()
        allowed_prefix = os.getenv(
            "SURVEYCTO_WEBHOOK_RESPONDENT_PREFIX",
            DEFAULT_RESPONDENT_NAME_PREFIX,
        ).strip()
        if not allowed_prefix or not respondent_name.startswith(allowed_prefix):
            return jsonify({"status": "ignored", "reason": "respondent_name_not_allowed"}), 202

        recipient = _request_email()
        if not recipient or "@" not in recipient:
            return jsonify({"error": "A valid report_user_email is required."}), 400

        job = WebhookJob(recipient=recipient, portal_origin=_portal_origin())
        state = _queue_test_pipeline(app, db_path, job)
        return jsonify({"status": "accepted", "pipeline": state, "recipient": recipient}), 202
