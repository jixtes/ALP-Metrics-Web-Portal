from __future__ import annotations

import os
import re
from collections.abc import Iterator

import requests
from dotenv import dotenv_values
from flask import Flask, Response, jsonify, request, stream_with_context
from requests.auth import HTTPBasicAuth

from .auth import authenticate_active_admin
from .service import PIPELINE_ROOT


FORM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def register_survey_relay_routes(app: Flask) -> None:
    @app.get("/api/client/surveys/<form_id>/raw.csv")
    def relay_survey(form_id: str) -> Response:
        return _relay_survey(form_id)


def _relay_survey(form_id: str) -> Response:
    if not FORM_ID_PATTERN.fullmatch(form_id) or form_id not in _allowed_form_ids():
        return _error_response("Survey not found.", 404)

    auth_error = _authorize_admin()
    if auth_error is not None:
        return auth_error

    config, config_error = _surveycto_config()
    if config_error is not None:
        return config_error

    server, username, password = config
    url = f"https://{server}.surveycto.com/api/v1/forms/data/wide/csv/{form_id}"

    try:
        upstream = requests.get(
            url,
            auth=HTTPBasicAuth(username, password),
            headers={"Accept": "text/csv", "Accept-Encoding": "identity"},
            stream=True,
            timeout=(10, 120),
            allow_redirects=False,
        )
    except requests.RequestException:
        return _error_response("The survey data provider is unavailable.", 502)

    if upstream.status_code != 200:
        upstream.close()
        return _error_response("The survey data provider returned an error.", 502)

    def response_body() -> Iterator[bytes]:
        try:
            yield from upstream.raw.stream(64 * 1024, decode_content=False)
        finally:
            upstream.close()

    response = Response(
        stream_with_context(response_body()),
        status=200,
        content_type=upstream.headers.get("Content-Type") or "text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{form_id}.csv"'
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _allowed_form_ids() -> set[str]:
    configured = os.getenv("SURVEY_RELAY_ALLOWED_FORMS", "")
    return {
        form_id
        for value in configured.split(",")
        if (form_id := value.strip()) and FORM_ID_PATTERN.fullmatch(form_id)
    }


def _authorize_admin() -> Response | None:
    credentials = request.authorization
    if not credentials or credentials.type.lower() != "basic":
        return _unauthorized_response()
    if not authenticate_active_admin(credentials.username or "", credentials.password or ""):
        return _unauthorized_response()
    return None


def _surveycto_config() -> tuple[tuple[str, str, str], None] | tuple[None, Response]:
    pipeline_env = dotenv_values(PIPELINE_ROOT / ".env")
    values = (
        str(os.getenv("SURVEYCTO_SERVER") or pipeline_env.get("SURVEYCTO_SERVER") or "").strip(),
        str(os.getenv("SURVEYCTO_USERNAME") or pipeline_env.get("SURVEYCTO_USERNAME") or "").strip(),
        str(os.getenv("SURVEYCTO_PASSWORD") or pipeline_env.get("SURVEYCTO_PASSWORD") or ""),
    )
    if not all(values):
        return None, _error_response("Survey relay is not configured.", 503)
    return values, None


def _unauthorized_response() -> Response:
    response = _error_response("Valid administrator credentials are required.", 401)
    response.headers["WWW-Authenticate"] = 'Basic realm="ALP Metrics survey relay", charset="UTF-8"'
    return response


def _error_response(message: str, status: int) -> Response:
    response = jsonify({"error": message})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
