from __future__ import annotations

import json
from pathlib import Path

from dotenv import dotenv_values

from powerbi.client import PowerBIClient, PowerBIConfig

ENV_PATH = Path(".env")


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
    config = PowerBIConfig.from_env(ENV_PATH)

    print("Environment check")
    print(f"  tenant: {masked(config.tenant_id)}")
    print(f"  client: {masked(config.client_id)}")
    print(f"  secret: {masked(config.client_secret)}")
    print(f"  workspace: {masked(env.get('POWERBI_WORKSPACE_ID'))}")
    print(f"  report: {masked(env.get('POWERBI_REPORT_ID'))}")
    print(f"  dataset: {masked(env.get('POWERBI_DATASET_ID'))}")

    try:
        client = PowerBIClient(config)
        workspaces = client.list_workspaces()
    except Exception as exc:
        print("\nPower BI authentication failed")
        print(f"  error: {exc}")
        return

    print("\nAuthentication succeeded")
    print(f"  workspaces visible: {len(workspaces)}")

    if workspaces:
        preview = [
            {"id": workspace.get("id"), "name": workspace.get("name")}
            for workspace in workspaces[:10]
        ]
        print(json.dumps(preview, indent=2))

    if config.workspace_id:
        try:
            reports = client.list_reports()
        except Exception as exc:
            print("\nWorkspace lookup failed")
            print(f"  error: {exc}")
            return

        print(f"\nReports visible in configured workspace: {len(reports)}")
        preview = [
            {
                "id": report.get("id"),
                "name": report.get("name"),
                "datasetId": report.get("datasetId"),
            }
            for report in reports[:10]
        ]
        print(json.dumps(preview, indent=2))


if __name__ == "__main__":
    main()
