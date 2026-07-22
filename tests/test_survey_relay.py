from __future__ import annotations

import base64
import unittest
from unittest.mock import Mock, patch

from flask import Flask

from backend.survey_relay import register_survey_relay_routes


class SurveyRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = {
            "SURVEYCTO_SERVER": "example",
            "SURVEYCTO_USERNAME": "relay-user",
            "SURVEYCTO_PASSWORD": "relay-password",
            "SURVEY_RELAY_ALLOWED_FORMS": (
                "bayer_safe_handling_baseline_assessment,"
                "blfa_farmer_baseline_questionnaire"
            ),
        }
        app = Flask(__name__)
        register_survey_relay_routes(app)
        self.client = app.test_client()

    def basic_auth(self) -> dict[str, str]:
        value = base64.b64encode(b"admin@example.com:admin-password").decode()
        return {"Authorization": f"Basic {value}"}

    @patch("backend.survey_relay.authenticate_active_admin")
    @patch("backend.survey_relay.requests.get")
    def test_missing_credentials_are_rejected_without_calling_surveycto(
        self, request_get: Mock, authenticate_admin: Mock
    ) -> None:
        with patch.dict("os.environ", self.env, clear=False):
            response = self.client.get("/api/client/surveys/blfa_farmer_baseline_questionnaire/raw.csv")

        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers["WWW-Authenticate"])
        authenticate_admin.assert_not_called()
        request_get.assert_not_called()

    @patch("backend.survey_relay.authenticate_active_admin", return_value=True)
    @patch("backend.survey_relay.requests.get")
    def test_valid_admin_streams_the_allow_listed_form_unchanged(
        self, request_get: Mock, authenticate_admin: Mock
    ) -> None:
        body = b"KEY,project\nabc,BLFA\n"
        upstream = Mock()
        upstream.status_code = 200
        upstream.headers = {"Content-Type": "text/plain; charset=utf-8"}
        upstream.raw.stream.return_value = iter([body[:8], body[8:]])
        request_get.return_value = upstream

        with patch.dict("os.environ", self.env, clear=False):
            response = self.client.get(
                "/api/client/surveys/blfa_farmer_baseline_questionnaire/raw.csv",
                headers=self.basic_auth(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, body)
        self.assertEqual(response.headers["Cache-Control"], "no-store, private")
        self.assertEqual(
            response.headers["Content-Disposition"],
            'attachment; filename="blfa_farmer_baseline_questionnaire.csv"',
        )
        called_url = request_get.call_args.args[0]
        self.assertTrue(called_url.endswith("/blfa_farmer_baseline_questionnaire"))
        authenticate_admin.assert_called_once_with("admin@example.com", "admin-password")

    @patch("backend.survey_relay.authenticate_active_admin", return_value=True)
    @patch("backend.survey_relay.requests.get")
    def test_upstream_error_is_sanitized(self, request_get: Mock, authenticate_admin: Mock) -> None:
        upstream = Mock()
        upstream.status_code = 401
        request_get.return_value = upstream

        with patch.dict("os.environ", self.env, clear=False):
            response = self.client.get(
                "/api/client/surveys/bayer_safe_handling_baseline_assessment/raw.csv",
                headers=self.basic_auth(),
            )

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("SurveyCTO", response.get_data(as_text=True))
        upstream.close.assert_called_once()

    @patch("backend.survey_relay.authenticate_active_admin")
    @patch("backend.survey_relay.requests.get")
    def test_form_outside_environment_allow_list_returns_not_found(
        self, request_get: Mock, authenticate_admin: Mock
    ) -> None:
        with patch.dict("os.environ", self.env, clear=False):
            response = self.client.get(
                "/api/client/surveys/alp_metrics_survey_v3/raw.csv",
                headers=self.basic_auth(),
            )

        self.assertEqual(response.status_code, 404)
        authenticate_admin.assert_not_called()
        request_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
