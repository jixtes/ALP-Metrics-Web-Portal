"""Azure management client for the capacity used during portal refreshes."""
from __future__ import annotations

import re
import time
import requests

from .client import PowerBIConfig

RESOURCE_PATTERN = re.compile(
    r"^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/?#%]+/providers/Microsoft\.Fabric/capacities/[a-z0-9]+$",
    re.IGNORECASE,
)


def validate_resource_id(value: str) -> str:
    value = value.strip().rstrip("/")
    if not RESOURCE_PATTERN.fullmatch(value):
        raise ValueError("Enter the full Azure Resource ID of the Fabric capacity.")
    return value


class FabricAPIError(RuntimeError):
    def __init__(self, response):
        self.status_code = response.status_code
        try:
            payload = response.json()
            detail = payload.get("error", {}) if isinstance(payload, dict) else {}
        except ValueError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        self.code = str(detail.get("code") or "RequestFailed")
        messages = [str(detail.get("message") or response.reason or "Azure request failed")]
        for item in (detail.get("details") or [])[:3]:
            if isinstance(item, dict) and item.get("message"):
                messages.append(str(item["message"]))
        super().__init__(f"Azure Fabric {self.code} (HTTP {self.status_code}): " + " ".join(messages)[:1500])


class FabricClient:
    def __init__(self, resource_id: str):
        self.resource_id = validate_resource_id(resource_id)
        self.config = PowerBIConfig.from_env()
        self.token = None
        self.expires = 0

    def _request(self, method: str, **kwargs):
        if not self.token or time.time() >= self.expires:
            response = requests.post(self.config.token_url, data={
                "grant_type": "client_credentials", "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": "https://management.azure.com/.default",
            }, timeout=30)
            response.raise_for_status()
            payload = response.json()
            self.token = payload["access_token"]
            self.expires = time.time() + int(payload.get("expires_in", 3600)) - 120
        response = requests.request(method, "https://management.azure.com" + self.resource_id,
                                    params={"api-version": "2023-11-01"},
                                    headers={"Authorization": f"Bearer {self.token}"}, timeout=60, **kwargs)
        if not response.ok:
            raise FabricAPIError(response)
        return response

    def get(self) -> dict:
        return self._request("GET").json()

    def resize(self, sku: str) -> None:
        # F16 is retained solely to resume jobs started before the F32 rollout.
        if sku not in {"F2", "F16", "F32"}:
            raise ValueError("Only F2, F16, and F32 are supported for dashboard refresh.")
        self._request("PATCH", json={"sku": {"name": sku, "tier": "Fabric"}})
