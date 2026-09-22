"""Select a pipeline adapter; V2 and V3 implementations stay independent."""
from pathlib import Path
from typing import Any

from .pipelines import v2, v3

APP_DB_PATH = Path(__file__).resolve().parents[1] / "instance" / "alp_metrics.db"
# Kept for the existing V3 SurveyCTO relay.
PIPELINE_ROOT = v3.PIPELINE_ROOT


def normalize_pipeline_version(value: str = "V3") -> str:
    version = str(value).strip().upper()
    if version not in {"V2", "V3"}:
        raise ValueError("pipelineVersion must be V2 or V3.")
    return version


def _adapter(version: str):
    return v2 if normalize_pipeline_version(version) == "V2" else v3


def run_pipeline_and_snapshot(db_path: Path, *, pipeline_version: str = "V3", **kwargs: Any) -> dict:
    version = normalize_pipeline_version(pipeline_version)
    if version == "V3":
        # The isolated report webhook retains its explicit test mode.
        # Normal portal runs always extract fresh SurveyCTO data.
        if kwargs.get("extract_mode") != "surveycto_test":
            kwargs["extract_mode"] = "surveycto"
    return _adapter(version).run_pipeline_and_snapshot(db_path, **kwargs)


def get_pipeline_repo_status(pipeline_version: str = "V3") -> dict:
    return _adapter(pipeline_version).get_pipeline_repo_status()


def get_pipeline_commit_details(commit: str | None, pipeline_version: str = "V3") -> dict:
    return _adapter(pipeline_version).get_pipeline_commit_details(commit)


def pull_pipeline_repo(pipeline_version: str = "V3") -> dict:
    return _adapter(pipeline_version).pull_pipeline_repo()
