from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from flask import Flask
from flask_security import SQLAlchemyUserDatastore, hash_password

from backend.auth import Role, User, db, ensure_individual_report_account, init_auth
from backend.surveycto_webhook import (
    WebhookJob,
    _notify_job,
    _refresh_individual_report,
    _run_test_pipeline,
    register_surveycto_webhook_route,
)
import backend.surveycto_webhook as webhook_module


class IndividualReportAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        app = Flask(__name__, instance_path=self.temp_dir.name)
        app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{Path(self.temp_dir.name) / 'auth-test.db'}",
        )
        init_auth(app)
        self.app = app

    def tearDown(self) -> None:
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.temp_dir.cleanup()

    def test_missing_role_and_user_are_created(self) -> None:
        with self.app.app_context():
            role = Role.query.filter_by(name="individual_report_access").one()
            db.session.delete(role)
            db.session.commit()

            result = ensure_individual_report_account(
                "New.Person@Example.com",
                reset_origin="https://portal.example.com",
            )

            self.assertTrue(result["created"])
            self.assertEqual(result["user"].email, "new.person@example.com")
            self.assertEqual([role.name for role in result["user"].roles], ["individual_report_access"])
            self.assertTrue(result["reset"]["reset_url"].startswith("https://portal.example.com/reset-password?token="))

    def test_existing_user_and_roles_are_unchanged(self) -> None:
        with self.app.app_context():
            datastore = SQLAlchemyUserDatastore(db, User, Role)
            viewer = datastore.find_role("viewer")
            existing = datastore.create_user(
                email="existing@example.com",
                password=hash_password("existing-password"),
                active=True,
                roles=[viewer],
                allowed_project_refs_json="[]",
            )
            db.session.commit()

            result = ensure_individual_report_account(
                "existing@example.com",
                reset_origin="https://portal.example.com",
            )

            self.assertFalse(result["created"])
            self.assertEqual([role.name for role in existing.roles], ["viewer"])
            self.assertIsNone(result["reset"])

    def test_setup_token_is_invalid_after_password_is_created(self) -> None:
        with self.app.app_context():
            result = ensure_individual_report_account(
                "new.person@example.com",
                reset_origin="https://portal.example.com",
            )
            token = parse_qs(urlparse(result["reset"]["reset_url"]).query)["token"][0]

        client = self.app.test_client()
        csrf_token = client.get("/api/auth/session").get_json()["csrfToken"]
        reset_response = client.post(
            "/api/auth/reset-password",
            headers={"X-CSRF-Token": csrf_token},
            json={"token": token, "password": "new-password"},
        )
        reused_response = client.get(f"/api/auth/reset-password/validate?token={token}")

        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reused_response.status_code, 400)


class SurveyCTOWebhookRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.config["TESTING"] = True
        register_surveycto_webhook_route(app, Path("test.db"))
        self.client = app.test_client()

    @patch("backend.surveycto_webhook._queue_test_pipeline", return_value="started")
    def test_json_payload_uses_report_email(self, queue: Mock) -> None:
        with patch.dict("os.environ", {"SURVEYCTO_WEBHOOK_SECRET": "test-secret"}, clear=False):
            response = self.client.post(
                "/api/webhooks/surveycto?token=test-secret",
                json={
                    "report_user_email": " Person@Example.com ",
                    "resp_name_pl": "Jigsa Bulto Test Respondent",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["recipient"], "person@example.com")
        queued_job = queue.call_args.args[2]
        self.assertEqual(queued_job.recipient, "person@example.com")

    @patch("backend.surveycto_webhook._queue_test_pipeline", return_value="started")
    def test_nested_surveycto_payload_is_accepted(self, queue: Mock) -> None:
        with patch.dict("os.environ", {"SURVEYCTO_WEBHOOK_SECRET": "test-secret"}, clear=False):
            response = self.client.post(
                "/api/webhooks/surveycto?token=test-secret",
                json={
                    "data": {
                        "report_user_email": "person@example.com",
                        "resp_name_pl": "Jigsa Bulto Nested Test",
                    }
                },
            )

        self.assertEqual(response.status_code, 202)
        queue.assert_called_once()

    @patch("backend.surveycto_webhook._queue_test_pipeline")
    def test_invalid_token_is_rejected(self, queue: Mock) -> None:
        with patch.dict("os.environ", {"SURVEYCTO_WEBHOOK_SECRET": "test-secret"}, clear=False):
            response = self.client.post(
                "/api/webhooks/surveycto?token=wrong",
                json={
                    "report_user_email": "person@example.com",
                    "resp_name_pl": "Jigsa Bulto Test Respondent",
                },
            )

        self.assertEqual(response.status_code, 401)
        queue.assert_not_called()

    @patch("backend.surveycto_webhook._queue_test_pipeline", return_value="started")
    def test_missing_email_is_rejected_after_name_gate(self, queue: Mock) -> None:
        with patch.dict("os.environ", {"SURVEYCTO_WEBHOOK_SECRET": "test-secret"}, clear=False):
            response = self.client.post(
                "/api/webhooks/surveycto?token=test-secret",
                json={"resp_name_pl": "Jigsa Bulto Test Respondent"},
            )

        self.assertEqual(response.status_code, 400)
        queue.assert_not_called()

    @patch("backend.surveycto_webhook._queue_test_pipeline")
    def test_nonmatching_respondent_is_ignored(self, queue: Mock) -> None:
        with patch.dict("os.environ", {"SURVEYCTO_WEBHOOK_SECRET": "test-secret"}, clear=False):
            response = self.client.post(
                "/api/webhooks/surveycto?token=test-secret",
                json={
                    "report_user_email": "person@example.com",
                    "resp_name_pl": "Another Respondent",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "ignored")
        queue.assert_not_called()

    @patch("backend.surveycto_webhook._queue_test_pipeline", return_value="started")
    def test_legacy_request_email_remains_supported(self, queue: Mock) -> None:
        with patch.dict("os.environ", {"SURVEYCTO_WEBHOOK_SECRET": "test-secret"}, clear=False):
            response = self.client.post(
                "/api/webhooks/surveycto?token=test-secret",
                data={
                    "request_user_email": "legacy@example.com",
                    "resp_name_pl": "Jigsa Bulto Legacy Test",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["recipient"], "legacy@example.com")
        queue.assert_called_once()


class PowerBIRefreshTests(unittest.TestCase):
    @patch("backend.surveycto_webhook.time.sleep")
    @patch("backend.surveycto_webhook.PowerBIConfig.from_env")
    @patch("backend.surveycto_webhook.PowerBIClient")
    def test_refresh_waits_for_completed_status(
        self,
        client_class: Mock,
        powerbi_config: Mock,
        sleep: Mock,
    ) -> None:
        client = client_class.return_value
        client.list_reports.return_value = [
            {"id": "report-1", "name": "IR_PO_Baseline_v3_test", "datasetId": "dataset-1"}
        ]
        client.refresh_dataset.return_value = {"requestId": "refresh-2"}
        client.get_refresh_history.side_effect = [
            [{"requestId": "refresh-1", "status": "Completed"}],
            [{"requestId": "refresh-2", "status": "Unknown"}],
            [{"requestId": "refresh-2", "status": "Completed"}],
        ]

        result = _refresh_individual_report()

        self.assertEqual(result["refresh"]["status"], "Completed")
        client.refresh_dataset.assert_called_once_with(dataset_id="dataset-1")
        self.assertEqual(client.get_refresh_history.call_count, 3)
        sleep.assert_called_once()


class TestPipelineWorkflowTests(unittest.TestCase):
    def tearDown(self) -> None:
        with webhook_module._state_lock:
            webhook_module._pending_jobs.clear()
            webhook_module._recently_queued.clear()
            webhook_module._run_active = False

    def test_upload_and_refresh_complete_before_notification(self) -> None:
        events: list[str] = []
        app = Flask(__name__)
        job = WebhookJob("person@example.com", "https://portal.example.com")
        with webhook_module._state_lock:
            webhook_module._pending_jobs.append(job)
            webhook_module._run_active = True

        pipeline_result = {"uploads": [{"status": "uploaded"}]}
        with (
            patch(
                "backend.surveycto_webhook.run_pipeline_and_snapshot",
                side_effect=lambda *args, **kwargs: events.append("pipeline") or pipeline_result,
            ) as run_pipeline,
            patch(
                "backend.surveycto_webhook._refresh_individual_report",
                side_effect=lambda: events.append("refresh"),
            ),
            patch(
                "backend.surveycto_webhook._notify_job",
                side_effect=lambda queued_job: events.append("notify"),
            ),
        ):
            _run_test_pipeline(app, Path("test.db"))

        self.assertEqual(events, ["pipeline", "refresh", "notify"])
        self.assertEqual(run_pipeline.call_args.kwargs["extract_mode"], "surveycto_test")
        self.assertEqual(
            run_pipeline.call_args.kwargs["sharepoint_folder"],
            "ALP Metrics/Exports/local_update",
        )
        self.assertFalse(run_pipeline.call_args.kwargs["publish_snapshot"])

    @patch("backend.surveycto_webhook.Thread")
    def test_duplicate_recipient_is_not_queued_twice(self, thread_class: Mock) -> None:
        app = Flask(__name__)
        job = WebhookJob("person@example.com", "https://portal.example.com")

        first_state = webhook_module._queue_test_pipeline(app, Path("test.db"), job)
        second_state = webhook_module._queue_test_pipeline(app, Path("test.db"), job)

        self.assertEqual(first_state, "started")
        self.assertEqual(second_state, "duplicate")
        self.assertEqual(webhook_module._pending_jobs, [job])
        thread_class.assert_called_once()


class ReportNotificationTests(unittest.TestCase):
    @patch("backend.surveycto_webhook.send_report_ready_email")
    @patch("backend.surveycto_webhook.ensure_individual_report_account")
    def test_existing_user_is_not_modified_and_gets_report_link(
        self,
        ensure_account: Mock,
        send_email: Mock,
    ) -> None:
        ensure_account.return_value = {"created": False, "reset": None, "user": object()}
        send_email.return_value = Mock(sent=True, error=None)
        job = WebhookJob("person@example.com", "https://portal.example.com")

        _notify_job(job)

        ensure_account.assert_called_once_with(
            "person@example.com",
            reset_origin="https://portal.example.com",
        )
        send_email.assert_called_once_with(
            recipient="person@example.com",
            report_url="https://portal.example.com/individual-report",
        )

    @patch("backend.surveycto_webhook.send_report_ready_setup_email")
    @patch("backend.surveycto_webhook.send_report_ready_email")
    @patch("backend.surveycto_webhook.ensure_individual_report_account")
    def test_new_user_gets_report_ready_password_setup_email(
        self,
        ensure_account: Mock,
        send_report_email: Mock,
        send_setup_email: Mock,
    ) -> None:
        ensure_account.return_value = {
            "created": True,
            "user": object(),
            "reset": {
                "reset_url": "https://portal.example.com/reset-password?token=abc",
                "expires_at": Mock(isoformat=Mock(return_value="2026-09-02T08:00:00+00:00")),
            },
        }
        send_setup_email.return_value = Mock(sent=True, error=None)

        _notify_job(WebhookJob("new@example.com", "https://portal.example.com"))

        send_setup_email.assert_called_once_with(
            recipient="new@example.com",
            reset_url="https://portal.example.com/reset-password?token=abc",
            expires_at="2026-09-02T08:00:00+00:00",
        )
        send_report_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
