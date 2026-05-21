from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

LOGIN_BASE_URL = "https://login.microsoftonline.com"
POWER_BI_API_BASE_URL = "https://api.powerbi.com/v1.0/myorg"
POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


@dataclass
class PowerBIConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    workspace_id: str
    report_id: str
    dataset_id: str
    default_effective_username: str
    default_effective_roles: list[str]

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "PowerBIConfig":
        load_dotenv(env_path)
        return cls(
            tenant_id=(os.getenv("POWERBI_TENANT_ID") or os.getenv("MICROSOFT_TENANT_ID") or "").strip(),
            client_id=(os.getenv("POWERBI_CLIENT_ID") or os.getenv("MICROSOFT_CLIENT_ID") or "").strip(),
            client_secret=(os.getenv("POWERBI_CLIENT_SECRET") or os.getenv("MICROSOFT_CLIENT_SECRET") or "").strip(),
            workspace_id=os.getenv("POWERBI_WORKSPACE_ID", "").strip(),
            report_id=os.getenv("POWERBI_REPORT_ID", "").strip(),
            dataset_id=os.getenv("POWERBI_DATASET_ID", "").strip(),
            default_effective_username=(
                os.getenv("POWERBI_DEFAULT_EFFECTIVE_USERNAME")
                or "JigsaBulto@alpmetrics497.onmicrosoft.com"
            ).strip(),
            default_effective_roles=[
                role.strip()
                for role in (os.getenv("POWERBI_DEFAULT_EFFECTIVE_ROLES") or "AllProjects").split(",")
                if role.strip()
            ],
        )

    @cached_property
    def token_url(self) -> str:
        return f"{LOGIN_BASE_URL}/{self.tenant_id}/oauth2/v2.0/token"

    def validate_basic(self) -> None:
        missing = [
            name
            for name, value in (
                ("POWERBI_TENANT_ID", self.tenant_id),
                ("POWERBI_CLIENT_ID", self.client_id),
                ("POWERBI_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required Power BI settings: {', '.join(missing)}")


class PowerBIClient:
    def __init__(self, config: PowerBIConfig) -> None:
        self.config = config
        self.config.validate_basic()
        self._access_token: str | None = None

    def list_workspaces(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/groups").json()
        return payload.get("value", [])

    def get_workspace(self, workspace_id: str | None = None) -> dict[str, Any]:
        target_workspace_id = workspace_id or self.config.workspace_id
        if not target_workspace_id:
            raise ValueError("workspace_id is required for get_workspace().")
        return self._request("GET", f"/groups/{target_workspace_id}").json()

    def list_reports(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        target_workspace_id = workspace_id or self.config.workspace_id
        if not target_workspace_id:
            raise ValueError("workspace_id is required for list_reports().")
        payload = self._request("GET", f"/groups/{target_workspace_id}/reports").json()
        return payload.get("value", [])

    def get_report(self, report_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        reports = self.list_reports(workspace_id=workspace_id)
        report = next((item for item in reports if item.get("id") == report_id), None)
        if report is None:
            raise ValueError(f"Report '{report_id}' was not found in the target workspace.")
        return report

    def get_dataset(self, dataset_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        target_workspace_id = workspace_id or self.config.workspace_id
        if not target_workspace_id:
            raise ValueError("workspace_id is required for get_dataset().")
        return self._request("GET", f"/groups/{target_workspace_id}/datasets/{dataset_id}").json()

    def refresh_dataset(self, dataset_id: str | None = None, workspace_id: str | None = None) -> dict[str, Any]:
        target_workspace_id = workspace_id or self.config.workspace_id
        target_dataset_id = dataset_id or self.config.dataset_id
        missing = [
            name
            for name, value in (
                ("workspace_id", target_workspace_id),
                ("dataset_id", target_dataset_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing values for dataset refresh: {', '.join(missing)}")

        response = self._request(
            "POST",
            f"/groups/{target_workspace_id}/datasets/{target_dataset_id}/refreshes",
            json={},
        )
        return {
            "datasetId": target_dataset_id,
            "statusCode": response.status_code,
            "refreshUrl": response.headers.get("Location"),
            "requestId": response.headers.get("RequestId") or response.headers.get("x-ms-request-id"),
        }

    def generate_embed_token(
        self,
        *,
        workspace_id: str | None = None,
        report_id: str | None = None,
        dataset_id: str | None = None,
        username: str | None = None,
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        target_workspace_id = workspace_id or self.config.workspace_id
        target_report_id = report_id or self.config.report_id
        target_dataset_id = dataset_id or self.config.dataset_id
        missing = [
            name
            for name, value in (
                ("workspace_id", target_workspace_id),
                ("report_id", target_report_id),
                ("dataset_id", target_dataset_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing values for embed token generation: {', '.join(missing)}")

        body = {
            "reports": [{"id": target_report_id}],
            "datasets": [{"id": target_dataset_id}],
            "targetWorkspaces": [{"id": target_workspace_id}],
        }
        if username:
            body["identities"] = [
                {
                    "username": username,
                    "datasets": [target_dataset_id],
                    **({"roles": roles} if roles else {}),
                }
            ]
        return self._request("POST", "/GenerateToken", json=body).json()

    def build_embed_config(
        self,
        *,
        workspace_id: str | None = None,
        report_id: str | None = None,
        dataset_id: str | None = None,
        username: str | None = None,
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        target_workspace_id = workspace_id or self.config.workspace_id
        target_report_id = report_id or self.config.report_id
        target_dataset_id = dataset_id or self.config.dataset_id
        reports = self.list_reports(target_workspace_id)
        report = next((item for item in reports if item.get("id") == target_report_id), None)
        if report is None:
            raise ValueError(
                "The configured report was not found in the target workspace. "
                "Set POWERBI_WORKSPACE_ID and POWERBI_REPORT_ID after you verify access."
            )

        effective_username = None
        effective_roles = None
        if target_dataset_id:
            dataset = self.get_dataset(target_dataset_id, workspace_id=target_workspace_id)
            if dataset.get("isEffectiveIdentityRequired"):
                effective_username = username or self.config.default_effective_username
                if dataset.get("isEffectiveIdentityRolesRequired"):
                    effective_roles = roles or self.config.default_effective_roles

        token_payload = self.generate_embed_token(
            workspace_id=target_workspace_id,
            report_id=target_report_id,
            dataset_id=target_dataset_id,
            username=effective_username,
            roles=effective_roles,
        )

        return {
            "type": "report",
            "reportId": report["id"],
            "reportName": report.get("name"),
            "embedUrl": report.get("embedUrl"),
            "datasetId": report.get("datasetId"),
            "accessToken": token_payload["token"],
            "tokenExpiration": token_payload.get("expiration"),
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token_value()}"
        headers.setdefault("Content-Type", "application/json")
        response = requests.request(
            method,
            f"{POWER_BI_API_BASE_URL}{path}",
            headers=headers,
            timeout=120,
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
                    "scope": POWER_BI_SCOPE,
                },
                timeout=60,
            )
            response.raise_for_status()
            self._access_token = response.json()["access_token"]
        return self._access_token
