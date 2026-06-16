from __future__ import annotations

import os
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import quote

import requests


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
LOGIN_BASE_URL = "https://login.microsoftonline.com"


@dataclass(frozen=True)
class EmailResult:
    attempted: bool
    sent: bool
    error: str | None = None


@dataclass(frozen=True)
class GraphMailConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    sender: str
    enabled: bool

    @classmethod
    def from_env(cls) -> "GraphMailConfig":
        return cls(
            tenant_id=(os.getenv("MAIL_TENANT_ID") or os.getenv("MICROSOFT_TENANT_ID") or "").strip(),
            client_id=(os.getenv("MAIL_CLIENT_ID") or os.getenv("MICROSOFT_CLIENT_ID") or "").strip(),
            client_secret=(os.getenv("MAIL_CLIENT_SECRET") or os.getenv("MICROSOFT_CLIENT_SECRET") or "").strip(),
            sender=(os.getenv("MAIL_SENDER") or "").strip(),
            enabled=os.getenv("MAIL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        )

    @property
    def token_url(self) -> str:
        return f"{LOGIN_BASE_URL}/{self.tenant_id}/oauth2/v2.0/token"

    def missing_values(self) -> list[str]:
        return [
            name
            for name, value in (
                ("MAIL_SENDER", self.sender),
                ("MICROSOFT_TENANT_ID or MAIL_TENANT_ID", self.tenant_id),
                ("MICROSOFT_CLIENT_ID or MAIL_CLIENT_ID", self.client_id),
                ("MICROSOFT_CLIENT_SECRET or MAIL_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]


class GraphMailClient:
    def __init__(self, config: GraphMailConfig) -> None:
        self.config = config
        self._access_token: str | None = None

    def send_password_reset(self, *, recipient: str, reset_url: str, expires_at: str) -> EmailResult:
        if not self.config.enabled:
            return EmailResult(attempted=False, sent=False, error="Email sending is disabled.")

        missing = self.config.missing_values()
        if missing:
            return EmailResult(attempted=False, sent=False, error=f"Missing email settings: {', '.join(missing)}.")

        escaped_reset_url = escape(reset_url, quote=True)
        escaped_expires_at = escape(expires_at)

        subject = "Set up your ALP Metrics account"
        html_body = (
            '<div style="margin:0;padding:0;background:#f6f8fb;font-family:Arial,sans-serif;color:#18212f;">'
            '<div style="max-width:560px;margin:0 auto;padding:32px 20px;">'
            '<div style="background:#ffffff;border:1px solid #dce3ee;border-radius:8px;padding:28px;">'
            '<p style="margin:0 0 8px 0;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#546179;">'
            "ALP Metrics Portal"
            "</p>"
            '<h1 style="margin:0 0 16px 0;font-size:24px;line-height:1.25;color:#111827;">'
            "Set up your account"
            "</h1>"
            '<p style="margin:0 0 18px 0;font-size:15px;line-height:1.6;color:#334155;">'
            "An ALP Metrics account has been created for you. Use the button below to choose your password "
            "and sign in to the portal."
            "</p>"
            '<table role="presentation" cellspacing="0" cellpadding="0" style="margin:24px 0;border-collapse:separate;">'
            "<tr>"
            '<td bgcolor="#183c36" style="border-radius:14px;background:#183c36;">'
            f'<a href="{escaped_reset_url}" '
            'style="display:inline-block;color:#ffffff;text-decoration:none;font-weight:700;'
            'border-radius:14px;padding:14px 22px;font-size:15px;line-height:1.2;'
            'background:#183c36;">'
            "Set password"
            "</a>"
            "</td>"
            "</tr>"
            "</table>"
            '<p style="margin:0 0 18px 0;font-size:14px;line-height:1.6;color:#475569;">'
            f"This link expires on <strong>{escaped_expires_at}</strong>. For security, it can only be used once."
            "</p>"
            '<div style="border-top:1px solid #e5eaf2;margin:22px 0 0 0;padding-top:18px;">'
            '<p style="margin:0 0 8px 0;font-size:13px;line-height:1.5;color:#64748b;">'
            "If the button does not work, copy and paste this link into your browser:"
            "</p>"
            f'<p style="margin:0;word-break:break-all;font-size:13px;line-height:1.5;color:#183c36;">{escaped_reset_url}</p>'
            "</div>"
            "</div>"
            '<p style="margin:16px 0 0 0;font-size:12px;line-height:1.5;color:#64748b;">'
            "If you were not expecting this email, you can ignore it."
            "</p>"
            "</div>"
            "</div>"
        )

        try:
            self._request(
                "POST",
                f"/users/{quote(self.config.sender)}/sendMail",
                json={
                    "message": {
                        "subject": subject,
                        "body": {
                            "contentType": "HTML",
                            "content": html_body,
                        },
                        "toRecipients": [{"emailAddress": {"address": recipient}}],
                    },
                    "saveToSentItems": False,
                },
            )
            return EmailResult(attempted=True, sent=True)
        except Exception as exc:
            return EmailResult(attempted=True, sent=False, error=str(exc))

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token_value()}"
        headers.setdefault("Content-Type", "application/json")
        response = requests.request(
            method,
            f"{GRAPH_BASE_URL}{path}",
            headers=headers,
            timeout=60,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def _access_token_value(self) -> str:
        if self._access_token is None:
            response = requests.post(
                self.config.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "scope": GRAPH_SCOPE,
                },
                timeout=60,
            )
            response.raise_for_status()
            self._access_token = response.json()["access_token"]
        return self._access_token


def send_password_reset_email(*, recipient: str, reset_url: str, expires_at: str) -> EmailResult:
    client = GraphMailClient(GraphMailConfig.from_env())
    return client.send_password_reset(recipient=recipient, reset_url=reset_url, expires_at=expires_at)
