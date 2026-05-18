from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import dotenv_values


ENV_PATH = Path(".env")
TOKEN_SCOPE = "https://graph.microsoft.com/.default"


def masked(value: str | None, keep: int = 4) -> str:
    if not value:
        return "<missing>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def main() -> None:
    if not ENV_PATH.exists():
        print("Missing .env file in repository root.")
        return

    env = dotenv_values(ENV_PATH)
    tenant_id = (env.get("MICROSOFT_TENANT_ID") or "").strip()
    client_id = (env.get("MICROSOFT_CLIENT_ID") or "").strip()
    client_secret = (env.get("MICROSOFT_CLIENT_SECRET") or "").strip()
    site_url = (env.get("SHAREPOINT_SITE_URL") or "").strip()

    print("Environment check")
    print(f"  tenant: {masked(tenant_id)} len={len(tenant_id)}")
    print(f"  client: {masked(client_id)} len={len(client_id)}")
    print(f"  secret: {masked(client_secret)} len={len(client_secret)}")
    print(f"  site: {site_url or '<missing>'}")

    if not tenant_id or not client_id or not client_secret:
        print("\nOne or more required Microsoft Graph values are missing in .env.")
        return

    tenants_to_try = [tenant_id]
    guessed_tenant = guess_tenant_from_site_url(site_url)
    if guessed_tenant and guessed_tenant not in tenants_to_try:
        tenants_to_try.append(guessed_tenant)

    for candidate in tenants_to_try:
        test_tenant(candidate, client_id, client_secret)


def guess_tenant_from_site_url(site_url: str) -> str | None:
    if not site_url:
        return None
    hostname = urlparse(site_url).netloc
    if not hostname.endswith(".sharepoint.com"):
        return None
    prefix = hostname.removesuffix(".sharepoint.com")
    if not prefix:
        return None
    return f"{prefix}.onmicrosoft.com"


def test_tenant(tenant: str, client_id: str, client_secret: str) -> None:
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    print(f"\nToken URL\n  {token_url}")

    response = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": TOKEN_SCOPE,
        },
        timeout=60,
    )

    print(f"\nHTTP status\n  {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        print("\nRaw response")
        print(response.text)
        return

    if response.ok:
        access_token = payload.get("access_token", "")
        print("\nAuthentication succeeded")
        print(f"  token received: {masked(access_token, keep=10)}")
        print(f"  expires_in: {payload.get('expires_in')}")
        return

    print("\nAuthentication failed")
    print(f"  error: {payload.get('error')}")
    print(f"  description: {payload.get('error_description')}")
    print(f"  trace_id: {payload.get('trace_id')}")
    print(f"  correlation_id: {payload.get('correlation_id')}")


if __name__ == "__main__":
    main()
