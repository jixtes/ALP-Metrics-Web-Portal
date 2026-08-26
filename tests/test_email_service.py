from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.email_service import GraphMailClient, GraphMailConfig


class ReportReadySetupEmailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GraphMailClient(
            GraphMailConfig(
                tenant_id="tenant",
                client_id="client",
                client_secret="secret",
                sender="sender@example.com",
                enabled=True,
            )
        )

    @patch.object(GraphMailClient, "_request")
    def test_report_ready_setup_button_says_go_to_report(self, request) -> None:
        result = self.client.send_password_reset(
            recipient="new.user@example.com",
            reset_url="https://portal.example.com/reset-password?token=test",
            expires_at="2026-09-02T08:00:00+00:00",
            report_ready=True,
        )

        html = request.call_args.kwargs["json"]["message"]["body"]["content"]
        self.assertTrue(result.sent)
        self.assertIn("Go to report", html)
        self.assertNotIn(">Set password<", html)

    @patch.object(GraphMailClient, "_request")
    def test_standard_reset_button_still_says_set_password(self, request) -> None:
        result = self.client.send_password_reset(
            recipient="existing.user@example.com",
            reset_url="https://portal.example.com/reset-password?token=test",
            expires_at="2026-09-02T08:00:00+00:00",
        )

        html = request.call_args.kwargs["json"]["message"]["body"]["content"]
        self.assertTrue(result.sent)
        self.assertIn("Set password", html)


if __name__ == "__main__":
    unittest.main()
