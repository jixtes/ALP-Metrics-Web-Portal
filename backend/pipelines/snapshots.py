from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

SUMMARY_COLUMNS = {
    "survey_name": "project",
    "project_ref": "project_ref_pl",
    "project_label": "project_label_pl",
    "client": "client_pl",
    "country": "country_pl",
    "phase": "phase_pl",
    "cohort": "cohort_pl",
    "assessor": "assessor_pl",
    "trc": "trc_pl",
    "fpa": "fpa_pl",
    "blr": "blr_pl",
    "submission_date": "SubmissionDate",
    "submission_key": "id_key",
    "enumerator": "enumerator",
    "respondent_name": "resp_name_pl",
    "entity_type": "entity_type_eng_pl",
    "target_group": "entity_target_group_eng_pl",
}

SURVEY_PREVIEW_FIELDS = [
    "survey_name",
    "project_ref",
    "project_label",
    "client",
    "country",
    "phase",
    "cohort",
    "assessor",
    "trc",
    "fpa",
    "blr",
]

RECORD_PREVIEW_FIELDS = [
    "submission_key",
    "submission_date",
    "enumerator",
    "respondent_name",
    "country",
    "entity_type",
    "target_group",
]

RECENT_DAYS_LIMIT = 7
ENUMERATOR_DAILY_LIMIT = 50


def build_snapshot_rows(export_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dataframe = pd.read_csv(export_path, encoding="utf-8-sig")
    return build_snapshot_dataframe(dataframe)


def build_snapshot_dataframe(dataframe: pd.DataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized = _normalize_dataframe(dataframe)
    if normalized.empty:
        return [], []

    normalized["submission_date"] = pd.to_datetime(normalized["submission_date"], errors="coerce", format="mixed", utc=True)
    survey_rows: list[dict[str, Any]] = []
    record_rows: list[dict[str, Any]] = []

    grouped = normalized.groupby("survey_name", dropna=False)
    for survey_name, group in grouped:
        non_null_group = group.copy()
        non_null_group = non_null_group.sort_values("submission_date", ascending=False, na_position="last")

        survey_rows.append(
            {
                "survey_name": _stringify(survey_name),
                "project_ref": _first_non_null(group["project_ref"]),
                "project_label": _first_non_null(group["project_label"]),
                "client": _first_non_null(group["client"]),
                "country": _first_non_null(group["country"]),
                "phase": _first_non_null(group["phase"]),
                "cohort": _first_non_null(group["cohort"]),
                "assessor": _first_non_null(group["assessor"]),
                "trc": _int_or_none(_first_non_null(group["trc"])),
                "fpa": _int_or_none(_first_non_null(group["fpa"])),
                "blr": _int_or_none(_first_non_null(group["blr"])),
                "submission_count": int(len(group.index)),
                "first_submission_at": _datetime_to_iso(group["submission_date"].min()),
                "last_submission_at": _datetime_to_iso(group["submission_date"].max()),
                "preview": {
                    field: _preview_value(non_null_group.iloc[0][field]) if field in non_null_group.columns else None
                    for field in SURVEY_PREVIEW_FIELDS
                }
                | {
                    "daily_submission_counts": _daily_submission_counts(group),
                    "entity_daily_counts": _entity_daily_counts(group),
                    "enumerator_daily_counts": _enumerator_daily_counts(group),
                    "entity_category_counts": _entity_category_counts(group),
                    "active_enumerator_count": _active_enumerator_count(group["enumerator"]),
                    "entity_type_count": _entity_type_count(group["entity_type"]),
                    "entity_type_totals": _value_totals(group["entity_type"]),
                    "most_entity_types": _most_common_values(group["entity_type"]),
                    "most_target_groups": _most_common_values(group["target_group"]),
                },
            }
        )

    survey_rows.sort(key=lambda item: (-item["submission_count"], item["survey_name"]))
    return survey_rows, record_rows


def _normalize_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
    renamed = dataframe.rename(columns={source: target for target, source in SUMMARY_COLUMNS.items() if source in dataframe.columns})
    normalized = renamed.reindex(columns=list(SUMMARY_COLUMNS)).copy()
    normalized["country"] = normalized["country"].map(_normalize_country)
    return normalized


def _normalize_country(value: Any) -> Any:
    text = _stringify(value)
    if text == "15":
        return "Nigeria"
    return value


def _first_non_null(series: pd.Series) -> str | None:
    for value in series:
        if pd.notna(value) and str(value).strip():
            return str(value).strip()
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _stringify(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _preview_value(value: Any) -> str | int | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if float(value).is_integer() else float(value)
    return str(value).strip() or None


def _most_common_values(series: pd.Series, limit: int = 3) -> list[str]:
    cleaned = series.map(_stringify).dropna()
    if cleaned.empty:
        return []

    counts = cleaned.value_counts()
    return [f"{value} ({int(count)})" for value, count in counts.head(limit).items()]


def _value_totals(series: pd.Series) -> list[str]:
    cleaned = series.map(_stringify).dropna()
    if cleaned.empty:
        return []

    counts = cleaned.value_counts()
    return [f"{value} ({int(count)})" for value, count in counts.items() if int(count) > 0]


def _daily_submission_counts(group: pd.DataFrame, limit: int = RECENT_DAYS_LIMIT) -> list[dict[str, Any]]:
    dated = group["submission_date"].dropna()
    if dated.empty:
        return []

    counts = dated.dt.date.value_counts().sort_index(ascending=False).head(limit)
    return [{"date": date.isoformat(), "count": int(count)} for date, count in counts.items()]


def _enumerator_daily_counts(group: pd.DataFrame, limit: int = ENUMERATOR_DAILY_LIMIT) -> list[dict[str, Any]]:
    dated = group[["submission_date", "enumerator"]].dropna(subset=["submission_date"]).copy()
    if dated.empty:
        return []

    recent_dates = dated["submission_date"].dt.date.drop_duplicates().sort_values(ascending=False).head(RECENT_DAYS_LIMIT)
    dated["date"] = dated["submission_date"].dt.date
    dated["enumerator"] = dated["enumerator"].map(_stringify).fillna("Unknown enumerator")
    dated = dated[dated["date"].isin(set(recent_dates))]
    counts = (
        dated.groupby(["date", "enumerator"], dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["date", "count", "enumerator"], ascending=[False, False, True])
        .head(limit)
    )

    return [
        {"date": row["date"].isoformat(), "enumerator": row["enumerator"], "count": int(row["count"])}
        for _, row in counts.iterrows()
    ]


def _entity_daily_counts(group: pd.DataFrame, limit: int = ENUMERATOR_DAILY_LIMIT) -> list[dict[str, Any]]:
    dated = group[["submission_date", "entity_type"]].dropna(subset=["submission_date"]).copy()
    if dated.empty:
        return []

    recent_dates = dated["submission_date"].dt.date.drop_duplicates().sort_values(ascending=False).head(RECENT_DAYS_LIMIT)
    dated["date"] = dated["submission_date"].dt.date
    dated["entity_type"] = dated["entity_type"].map(_stringify).fillna("Unknown entity type")
    dated = dated[dated["date"].isin(set(recent_dates))]
    counts = (
        dated.groupby(["date", "entity_type"], dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["date", "count", "entity_type"], ascending=[False, False, True])
        .head(limit)
    )

    return [
        {"date": row["date"].isoformat(), "entity_type": row["entity_type"], "count": int(row["count"])}
        for _, row in counts.iterrows()
    ]


def _entity_category_counts(group: pd.DataFrame) -> dict[str, int]:
    counts = {"pos": 0, "retailers": 0, "lead_farmers": 0}
    for _, row in group.iterrows():
        category = _entity_category_key(row.get("entity_type"), row.get("target_group"))
        if category:
            counts[category] += 1
    return counts


def _entity_category_key(entity_type: Any, target_group: Any = None) -> str | None:
    values = [_stringify(entity_type), _stringify(target_group)]
    haystack = " ".join(value.lower() for value in values if value)
    entity = (values[0] or "").lower()

    if entity == "po" or "producer organization" in haystack or "producer organisation" in haystack:
        return "pos"
    if entity == "rt" or "retail" in haystack:
        return "retailers"
    if entity == "lf" or "lead farmer" in haystack:
        return "lead_farmers"
    return None


def _active_enumerator_count(series: pd.Series) -> int:
    cleaned = series.map(_stringify).dropna()
    return int(cleaned.nunique())


def _entity_type_count(series: pd.Series) -> int:
    cleaned = series.map(_stringify).dropna()
    return int(cleaned.nunique())


def _datetime_to_iso(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        return timestamp.isoformat()
    return timestamp.tz_convert(timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
