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
        response.raise_for_status()
        return response

    def get(self) -> dict:
        return self._request("GET").json()

    def resize(self, sku: str) -> None:
        # F16 is retained solely to resume jobs started before the F32 rollout.
        if sku not in {"F2", "F16", "F32"}:
            raise ValueError("Only F2, F16, and F32 are supported for dashboard refresh.")
        self._request("PATCH", json={"sku": {"name": sku, "tier": "Fabric"}})
