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

## Repository Layout

```text
backend/         Flask routes, auth, SQLite dashboard storage, pipeline trigger service
frontend/        React/Vite interface
powerbi/         Power BI client and auth check helper
scripts/         operational checks such as SharePoint auth verification
```
