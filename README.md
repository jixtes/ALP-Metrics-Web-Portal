# ALP Metrics Web Portal

This repository contains the ALP Metrics web portal: the Flask backend, React
frontend, authentication screens, dashboard views, pipeline run controls, and
Power BI integration.

The pipeline code lives in a separate repository:

```text
https://github.com/jixtes/ALP-Metrics-V3-pipeline
```

Expected local folder layout:

```text
ALP-Metrics-Platform/
  alp-metrics-pipeline/  separate repository: pipeline code and exports
  web-portal/            this repository: web app and dashboard UI
```

## Setup

From the `web-portal` directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Install frontend dependencies:

```bash
cd frontend
npm install
```

## Pipeline Repository

The backend imports and runs the pipeline from a separate local checkout. By
default it looks for a sibling directory:

```text
../alp-metrics-pipeline
```

To use a different location, set:

```env
ALP_PIPELINE_REPO_PATH=/absolute/path/to/alp-metrics-pipeline
```

## Environment File

The web portal can use its own `.env` file for app, SharePoint, and Power BI
settings. Start from:

```bash
cp .env.example .env
```

Keep the pipeline `.env` in `../alp-metrics-pipeline/.env`; the notebook and
pipeline code load their credentials from the pipeline repo root.

Admins can manage the pipeline from Settings -> Pipeline:

- inspect the configured pipeline path, branch, commit, remote, and dirty state
- pull latest pipeline code with `git pull --ff-only`
- run the pipeline from the portal
- inspect the latest pull output and pipeline run log

Pulling is blocked when the pipeline repository has local uncommitted changes.

## Database Files

The web portal owns all application state:

```text
instance/alp_metrics.db
instance/auth.db
```

The pipeline writes exports only. It should not contain SQLite database files.

To verify Microsoft Graph credentials from the web portal environment:

```bash
python scripts/check_sharepoint_auth.py
```

## Password Reset Email

The admin user screen can send password reset emails through Microsoft Graph.
In Microsoft Entra, grant the app registration Microsoft Graph application
permission `Mail.Send`, then grant admin consent. Use a dedicated sender mailbox
such as `notifications@yourdomain.com`.

Configure the portal `.env`:

```env
MAIL_ENABLED=true
MAIL_SENDER=notifications@yourdomain.com
MICROSOFT_TENANT_ID=...
MICROSOFT_CLIENT_ID=...
MICROSOFT_CLIENT_SECRET=...
```

If email should use a separate Entra app, set `MAIL_TENANT_ID`,
`MAIL_CLIENT_ID`, and `MAIL_CLIENT_SECRET`; otherwise the Microsoft credentials
used elsewhere by the portal are reused. Keep `MAIL_ENABLED=false` locally to
create reset links without sending email.

## SurveyCTO Individual Report Webhook

SurveyCTO can trigger the test-only individual-report workflow with an HTTP
POST to:

```text
https://your-public-portal.example/api/webhooks/surveycto?token=YOUR_WEBHOOK_SECRET
```

The form submission must include `report_user_email` and `resp_name_pl`. JSON,
nested SurveyCTO JSON (`data.report_user_email`), and form-encoded payloads are
accepted. Only requests whose `resp_name_pl` begins with the configured prefix
are processed; other requests receive an HTTP 202 ignored response. Set these
portal environment values:

```env
SURVEYCTO_WEBHOOK_SECRET=replace-with-a-long-random-token
SURVEYCTO_WEBHOOK_DEDUP_SECONDS=300
SURVEYCTO_WEBHOOK_RESPONDENT_PREFIX=Jigsa Bulto
SHAREPOINT_INPUT_FOLDER=ALP Metrics/Exports/inputs
SURVEYCTO_TEST_SHAREPOINT_FOLDER=ALP Metrics/Exports/test_survey
PORTAL_PUBLIC_URL=https://your-public-portal.example
INDIVIDUAL_REPORT_NAME=IR_PO_Baseline_v3_test
POWERBI_REFRESH_TIMEOUT_SECONDS=900
POWERBI_REFRESH_POLL_SECONDS=5
```

The handler responds immediately with HTTP 202, then runs the
`surveycto_test` pipeline mode, uploads its isolated exports to the configured
`SURVEYCTO_TEST_SHAREPOINT_FOLDER`, requests a refresh of the report's Power BI
semantic model, and waits for a successful refresh. Only then does it create a
missing account and send the report-ready email. New accounts receive a
one-time password setup link and the `individual_report_access` role. Existing
accounts and their roles are left unchanged.

The individual report embed always sends the authenticated user's email as the
Power BI effective identity and uses the Power BI role `IR Web Demo User RLS`.
Portal users with `individual_report_access` are redirected to
`/individual-report` after login.

Test-survey uploads from the local `files/local_update` workspace and the
SharePoint `test_survey` folder are excluded from the portal's general Survey
data files table.

## Running Locally

Start the Flask API:

```bash
python run_backend.py
```

Start the Vite frontend:

```bash
cd frontend
npm run dev
```

The Vite dev server proxies API calls to Flask on `http://127.0.0.1:5000`.

To build the frontend for Flask to serve:

```bash
cd frontend
npm run build
```

After that, Flask serves the built app from `frontend/dist`.

## Raw SurveyCTO relay

The backend exposes two allow-listed, read-only CSV relay endpoints for approved
external clients:

```text
GET /api/client/surveys/bayer_safe_handling_baseline_assessment/raw.csv
GET /api/client/surveys/blfa_farmer_baseline_questionnaire/raw.csv
```

Requests use HTTP Basic authentication with an active ALP Metrics user assigned
the `raw_data_api_access` role:

```bash
curl --user 'client.api@example.com' \
  'https://portal.example.com/api/client/surveys/bayer_safe_handling_baseline_assessment/raw.csv'
```

SurveyCTO credentials are read from the pipeline repository's `.env`, just like
the notebook. The client never receives them. The form IDs that may be relayed
are configured as a comma-separated allow-list in the web portal environment:

```env
SURVEY_RELAY_ALLOWED_FORMS=bayer_safe_handling_baseline_assessment,blfa_farmer_baseline_questionnaire
```

Requests for form IDs outside this list return `404`. Production must enforce HTTPS,
because HTTP Basic credentials are included with every request. The API role is
created automatically and is restricted from portal login and normal workspace
data. Access can be revoked by disabling the user or changing its password.

## Repository Layout

```text
backend/         Flask routes, auth, SQLite dashboard storage, pipeline trigger service
frontend/        React/Vite interface
powerbi/         Power BI client and auth check helper
scripts/         operational checks such as SharePoint auth verification
```
