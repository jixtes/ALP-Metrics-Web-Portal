"""SurveyCTO preflight for V3 portal runs.

This deliberately lives in the portal integration. The V3 notebook and pipeline
continue to own normal extraction and processing when a run is required.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any, Callable

import requests
from dotenv import dotenv_values


SOURCES = (
    ("form", "alp_metrics_survey_v3"),
    ("form", "alp_metrics_survey_v3_promat"),
    ("dataset", "alp_metrics_survey_v3_wide_historic_data"),
)
_SERVER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")


def _credentials(root_dir: Path) -> tuple[str, str, str]:
    configured = dotenv_values(root_dir / ".env")
    values = {}
    for key in ("SURVEYCTO_SERVER", "SURVEYCTO_USERNAME", "SURVEYCTO_PASSWORD"):
        # The generated V3 extraction loads its repository .env with override=True.
        value = configured.get(key) if configured.get(key) is not None else os.getenv(key)
        values[key] = str(value or "")
    server = values["SURVEYCTO_SERVER"].strip()
    username = values["SURVEYCTO_USERNAME"].strip()
    password = values["SURVEYCTO_PASSWORD"]
    missing = [name for name, value in zip(
        ("SURVEYCTO_SERVER", "SURVEYCTO_USERNAME", "SURVEYCTO_PASSWORD"),
        (server, username, password),
    ) if not value or value.lower() == "none"]
    if missing:
        raise ValueError("Missing SurveyCTO setting(s) for V3 source check: " + ", ".join(missing))
    if not _SERVER_PATTERN.fullmatch(server):
        raise ValueError("SURVEYCTO_SERVER is not a valid SurveyCTO server name.")
    return server, username, password


def _source_url(server: str, source_type: str, source_id: str) -> str:
    if source_type == "form":
        path = f"api/v1/forms/data/wide/csv/{source_id}"
    elif source_type == "dataset":
        path = f"api/v2/datasets/data/csv/{source_id}"
    else:
        raise ValueError(f"Unknown SurveyCTO source type: {source_type}")
    return f"https://{server}.surveycto.com/{path}"


def _canonical_csv(payload: bytes, source_id: str) -> tuple[str, int]:
    """Hash CSV values while ignoring row and column ordering."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"SurveyCTO source {source_id} is not UTF-8 CSV data.") from exc
    reader = csv.reader(io.StringIO(text, newline=""))
    columns = next(reader, [])
    if not columns:
        raise ValueError(f"SurveyCTO source {source_id} returned an empty CSV export.")
    order = sorted(range(len(columns)), key=lambda index: (columns[index], index))
    row_hashes = []
    for row in reader:
        if len(row) != len(columns):
            raise ValueError(f"SurveyCTO source {source_id} returned an invalid CSV row.")
        canonical_row = [row[index] for index in order]
        row_hashes.append(hashlib.sha256(
            json.dumps(canonical_row, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest())
    value = [[columns[index] for index in order], sorted(row_hashes)]
    digest = hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return digest, len(row_hashes)


def surveycto_source_fingerprint(
    root_dir: Path,
    *,
    request_get: Callable[..., Any] = requests.get,
) -> dict[str, Any]:
    """Download the same V3 SurveyCTO sources and return one canonical fingerprint."""
    server, username, password = _credentials(Path(root_dir))
    source_versions = []
    for source_type, source_id in SOURCES:
        response = request_get(
            _source_url(server, source_type, source_id),
            auth=(username, password),
            timeout=120,
        )
        response.raise_for_status()
        fingerprint, row_count = _canonical_csv(response.content, source_id)
        source_versions.append({
            "type": source_type,
            "id": source_id,
            "fingerprint": fingerprint,
            "rowCount": row_count,
        })
    combined = hashlib.sha256(json.dumps(
        source_versions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {"fingerprint": combined, "sources": source_versions}
