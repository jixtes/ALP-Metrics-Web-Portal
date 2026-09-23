"""Durable, serialized refresh jobs. No remote actions happen until enabled in Settings."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time

from .database import connect_database
from powerbi.client import PowerBIClient, PowerBIConfig
from powerbi.fabric import FabricClient, validate_resource_id

TERMINAL = {"completed", "failed", "skipped"}
REFRESH_TERMINAL = {"Completed", "Failed", "Cancelled", "Canceled", "Disabled"}
_workers = {}
_worker_lock = threading.Lock()
log = logging.getLogger(__name__)


def initialize(db):
    with connect_database(db) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS powerbi_auto_settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS powerbi_data_versions (
                version TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, run_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS powerbi_refresh_watermarks (
                workspace TEXT, dataset TEXT, version TEXT, fingerprint TEXT NOT NULL,
                PRIMARY KEY(workspace, dataset, version));
            CREATE TABLE IF NOT EXISTS powerbi_refresh_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL);
        """)


def settings(db):
    with connect_database(db) as conn:
        row = conn.execute("SELECT value FROM powerbi_auto_settings WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {
        "reportIds": [], "reports": [], "capacityResourceId": os.getenv("FABRIC_CAPACITY_RESOURCE_ID", ""),
    }


def save_settings(db, payload, client=None, fabric_factory=FabricClient):
    ids = payload.get("reportIds")
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ValueError("reportIds must be a list of dashboard IDs.")
    ids = list(dict.fromkeys(ids))
    resource = str(payload.get("capacityResourceId") or "").strip()
    value = {"reportIds": ids, "reports": [], "capacityResourceId": resource}
    if ids:
        resource = validate_resource_id(resource)
        client = client or PowerBIClient(PowerBIConfig.from_env())
        reports = {r["id"]: r for r in client.list_reports()}
        if any(item not in reports or not reports[item].get("datasetId") for item in ids):
            raise ValueError("Select dashboards with semantic models from the configured workspace.")
        workspace = client.get_workspace()
        if not workspace.get("isOnDedicatedCapacity") or not workspace.get("capacityId"):
            raise ValueError("The Power BI workspace must be assigned to the configured Fabric capacity.")
        capacity = fabric_factory(resource).get()
        if capacity.get("sku", {}).get("name") != "F2" or capacity.get("properties", {}).get("state") != "Active":
            raise ValueError("The configured capacity must be active on F2 before enabling automatic refresh.")
        value.update(capacityResourceId=resource, workspaceId=client.config.workspace_id,
                     workspaceCapacityId=workspace["capacityId"], reports=[
                         {"id": item, "name": reports[item].get("name", item), "datasetId": reports[item]["datasetId"]}
                         for item in ids])
    with connect_database(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM powerbi_refresh_jobs WHERE status NOT IN ('completed','failed','skipped')").fetchone():
            raise ValueError("Wait for the current dashboard refresh and F2 restoration before changing these settings.")
        conn.execute("INSERT OR REPLACE INTO powerbi_auto_settings VALUES (1, ?)", (json.dumps(value),))
    return value


def csv_fingerprint(paths):
    """Ignore row/column ordering and file metadata; preserve duplicate rows and all field values."""
    files = []
    for logical_name, path in sorted(paths, key=lambda item: item[0]):
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            columns = next(reader, [])
            order = sorted(range(len(columns)), key=lambda i: (columns[i], i))
            rows = []
            for row in reader:
                if len(row) != len(columns):
                    raise ValueError(f"Invalid CSV row in {Path(path).name}; refresh not queued.")
                rows.append(hashlib.sha256(json.dumps([row[i] for i in order], ensure_ascii=False).encode()).hexdigest())
        files.append([logical_name, [columns[i] for i in order], sorted(rows)])
    if not files:
        raise ValueError("No uploaded survey CSV files were found for automatic refresh.")
    return hashlib.sha256(json.dumps(files, ensure_ascii=False).encode()).hexdigest()


def record_data_update(db, run_id, version, uploads, *, exports_root=None):
    config = settings(db)
    if not config.get("reportIds"):
        return
    # Only full successful uploads can trigger a refresh. Logs, QC, Excel metadata,
    # and individual report files are not survey-data change signals.
    if not uploads or any(row.get("status") != "uploaded" for row in uploads):
        return
    paths = []
    for row in uploads:
        path = Path(row["local_path"])
        if path.suffix.lower() != ".csv":
            continue
        if version == "V2":
            if not row.get("is_project_data"):
                continue
            logical = row.get("relative_path") or row["sharepoint_path"]
        else:
            try:
                logical = path.resolve().relative_to(Path(exports_root).resolve()).as_posix()
            except ValueError:
                continue
            if {part.lower() for part in Path(logical).parts}.intersection({"qc", "individual_reports", "test_survey"}):
                continue
        paths.append((logical, path))
    fingerprint = csv_fingerprint(paths)
    with connect_database(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Capture the latest saved selection while publishing the data generation.
        row = conn.execute("SELECT value FROM powerbi_auto_settings WHERE id=1").fetchone()
        config = json.loads(row[0]) if row else config
        conn.execute("INSERT OR REPLACE INTO powerbi_data_versions VALUES (?, ?, ?)", (version, fingerprint, run_id))
        targets = {}
        for report in config.get("reports", []):
            dataset = report["datasetId"]
            watermark = conn.execute("SELECT fingerprint FROM powerbi_refresh_watermarks WHERE workspace=? AND dataset=? AND version=?",
                                     (config["workspaceId"], dataset, version)).fetchone()
            if not watermark or watermark[0] != fingerprint:
                targets.setdefault(dataset, {"datasetId": dataset, "names": [], "state": "pending"})["names"].append(report["name"])
        value = {"config": config, "version": version, "fingerprint": fingerprint, "targets": list(targets.values()),
                 "created": time.time(), "stageStarted": time.time(), "ownsCapacity": False,
                 "message": "Waiting for the data update to finish." if targets else "No changed data; dashboard refresh skipped.",
                 "error": "", "retryAt": 0}
        conn.execute("INSERT OR IGNORE INTO powerbi_refresh_jobs (run_id,status,value,updated_at) VALUES (?,?,?,?)",
                     (run_id, "queued" if targets else "skipped", json.dumps(value), time.time()))


def latest_job(db):
    with connect_database(db) as conn:
        row = conn.execute("SELECT * FROM powerbi_refresh_jobs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    value = json.loads(row["value"])
    return {"id": row["id"], "runId": row["run_id"], "status": row["status"],
            "message": value["message"], "error": value.get("error", ""),
            "updatedAt": row["updated_at"], "active": row["status"] not in TERMINAL,
            "dashboards": [name for target in value["targets"] for name in target["names"]]}


def active_job(db):
    with connect_database(db) as conn:
        row = conn.execute("SELECT * FROM powerbi_refresh_jobs WHERE status NOT IN ('completed','failed','skipped') ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def _save(db, job, value, status=None):
    if status and status != job["status"]:
        job["status"] = status
        value["stageStarted"] = time.time()
    with connect_database(db) as conn:
        conn.execute("UPDATE powerbi_refresh_jobs SET status=?, value=?, updated_at=? WHERE id=?",
                     (job["status"], json.dumps(value), time.time(), job["id"]))


def _restore(db, job, value, error=""):
    value["error"] = error or value.get("error", "")
    value["message"] = "Restoring Fabric capacity to F2."
    _save(db, job, value, "restoring" if value["ownsCapacity"] else "failed")


def advance_job(db, job, *, fabric=None, client=None):
    """One resumable step. Persist intent before each non-idempotent remote operation."""
    value = json.loads(job["value"])
    if time.time() < value.get("retryAt", 0):
        return
    cfg = value["config"]
    try:
        fabric = fabric or FabricClient(cfg["capacityResourceId"])
        client = client or PowerBIClient(PowerBIConfig.from_env())
        # Retain the job's original workspace if deployment settings change mid-refresh.
        if job["status"] != "queued":
            client.config.workspace_id = cfg["workspaceId"]
        if job["status"] == "refreshing" and time.time() - value.get("boostStarted", value["created"]) > 21600:
            # Bound the expensive capacity window even if Power BI becomes unreachable.
            # Give known refreshes five minutes to cancel, then restore F2 and leave
            # unconfirmed data pending. Azure restoration itself keeps retrying.
            if not value.get("cancelStarted"):
                value["cancelStarted"] = time.time()
                _save(db, job, value)
            waiting = False
            for item in value["targets"]:
                if item["state"] in {"completed", "failed", "pending"}:
                    continue
                if not item.get("requestId"):
                    waiting = True
                    continue
                try:
                    history = client.get_refresh_history(item["datasetId"], top=60)
                    result = next((r for r in history if r.get("requestId") == item["requestId"]), None)
                    if result and result.get("status") in REFRESH_TERMINAL:
                        item["state"] = "completed" if result["status"] == "Completed" else "failed"
                        continue
                    waiting = True
                    client.cancel_refresh(item["datasetId"], item["requestId"])
                except Exception:
                    waiting = True
            if not waiting or time.time() - value["cancelStarted"] >= 300:
                _restore(db, job, value, "Automatic refresh exceeded the six-hour capacity boost limit; unfinished data remains pending.")
            else:
                value["message"] = "Capacity boost timed out; requesting refresh cancellation before restoring F2."
                _save(db, job, value)
            return

        if job["status"] == "queued":
            with connect_database(db) as conn:
                run = conn.execute("SELECT status FROM pipeline_runs WHERE id=?", (job["run_id"],)).fetchone()
            if not run or run[0] == "running":
                return
            if run[0] != "completed":
                value["message"] = "Data update was not fully successful; automatic refresh skipped."
                _save(db, job, value, "failed")
                return
            if client.config.workspace_id != cfg["workspaceId"] or client.get_workspace().get("capacityId") != cfg["workspaceCapacityId"]:
                raise ValueError("Power BI workspace or capacity changed. Save automatic refresh settings again.")
            actual = {r["id"]: r.get("datasetId") for r in client.list_reports()}
            if any(actual.get(r["id"]) != r["datasetId"] for r in cfg["reports"]):
                raise ValueError("A selected dashboard's semantic model changed. Save automatic refresh settings again.")
            cap = fabric.get()
            if cap.get("sku", {}).get("name") != "F2" or cap.get("properties", {}).get("state") != "Active":
                raise ValueError("Automatic refresh requires the capacity to start active on F2.")
            value["ownsCapacity"] = True
            value["boostStarted"] = time.time()
            value["message"] = "Scaling Fabric capacity to F16."
            _save(db, job, value, "scaling_up")
            # The next tick performs PATCH; a restart here resumes the recorded intent.
            return

        if job["status"] in {"scaling_up", "restoring"}:
            target = "F16" if job["status"] == "scaling_up" else "F2"
            cap = fabric.get()
            sku = cap.get("sku", {}).get("name")
            ready = cap.get("properties", {}).get("state") == "Active" and cap.get("properties", {}).get("provisioningState") == "Succeeded"
            if sku == target and ready:
                if target == "F16":
                    value["message"] = "Refreshing selected dashboards."
                    _save(db, job, value, "refreshing")
                else:
                    # A failed dataset stays pending even if other datasets completed.
                    with connect_database(db) as conn:
                        for item in value["targets"]:
                            if item["state"] == "completed":
                                conn.execute("INSERT OR REPLACE INTO powerbi_refresh_watermarks VALUES (?,?,?,?)",
                                             (cfg["workspaceId"], item["datasetId"], value["version"], value["fingerprint"]))
                        value["message"] = "Dashboard refresh failed; Fabric capacity restored to F2." if value["error"] else "Dashboards refreshed; Fabric capacity restored to F2."
                        job["status"] = "failed" if value["error"] else "completed"
                        conn.execute("UPDATE powerbi_refresh_jobs SET status=?,value=?,updated_at=? WHERE id=?",
                                     (job["status"], json.dumps(value), time.time(), job["id"]))
                return
            if target == "F16" and time.time() - value["stageStarted"] > 900:
                _restore(db, job, value, "Timed out while scaling to F16.")
                return
            if sku not in {"F2", "F16"}:
                raise RuntimeError("Capacity size changed outside this refresh. Waiting for F2 or F16 before continuing.")
            if ready:
                fabric.resize(target)
            value["retryAt"] = time.time() + 15
            _save(db, job, value)
            return

        if job["status"] == "refreshing":
            for item in value["targets"]:
                if item["state"] in {"completed", "failed"}:
                    continue
                history = client.get_refresh_history(item["datasetId"], top=60)
                if item["state"] == "pending":
                    if any(r.get("status") not in REFRESH_TERMINAL for r in history):
                        value["message"] = "Waiting for an existing semantic model refresh to finish."
                        if time.time() - value["stageStarted"] > 21600:
                            _restore(db, job, value, "Timed out waiting for an existing refresh.")
                        else:
                            _save(db, job, value)
                        return
                    item.update(state="submitting", before=[r.get("requestId") for r in history], submitted=time.time())
                    value["message"] = "Refreshing " + ", ".join(item["names"]) + "."
                    _save(db, job, value)
                    result = client.refresh_dataset(item["datasetId"])
                    request_id = result.get("requestId") or (result.get("refreshUrl") or "").rstrip("/").split("/")[-1]
                    if request_id:
                        item.update(state="running", requestId=request_id)
                    _save(db, job, value)
                    return
                # A crash/timeout after POST is reconciled from history, never blindly resubmitted.
                if item["state"] == "submitting":
                    # Without the POST response we cannot prove that a new history
                    # entry belongs to this job. Do not adopt or cancel another caller's refresh.
                    if time.time() - item["submitted"] > 300:
                        if any(r.get("status") not in REFRESH_TERMINAL for r in history):
                            value["message"] = "Refresh submission could not be confirmed; waiting for active refreshes before restoring F2."
                            _save(db, job, value)
                        else:
                            item["state"] = "failed"
                            _restore(db, job, value, "Refresh submission could not be confirmed. Changes remain pending for the next update.")
                    return
                result = next((r for r in history if r.get("requestId") == item["requestId"]), None)
                if result and result.get("status") in REFRESH_TERMINAL:
                    item["state"] = "completed" if result["status"] == "Completed" else "failed"
                    if item["state"] == "failed":
                        value["error"] = "Refresh failed for " + ", ".join(item["names"]) + "."
                    _save(db, job, value)
                return
            _restore(db, job, value)
    except Exception as exc:
        value["retryAt"] = time.time() + 30
        if job["status"] == "queued":
            value["message"] = "Automatic dashboard refresh could not start."
            value["error"] = str(exc)
            _save(db, job, value, "failed")
        elif job["status"] == "scaling_up" and time.time() - value["stageStarted"] > 900:
            _restore(db, job, value, "Unable to complete capacity scale-up: " + str(exc))
        else:
            # Persist recovery even when Azure/Power BI is unreachable. Never call an
            # unconfirmed restoration complete or lose its intent on process restart.
            value["message"] = ("F2 restoration pending; retrying automatically. " if job["status"] == "restoring"
                                else "Dashboard refresh is waiting for the service; retrying automatically. ") + str(exc)
            _save(db, job, value)


def start_worker(db):
    """One owner per SQLite DB across threads and Gunicorn processes; recover on startup."""
    db = Path(db).resolve()
    with _worker_lock:
        key = (str(db), os.getpid())
        if key in _workers and _workers[key].is_alive():
            return

        def work():
            import fcntl
            while True:
                try:
                    with open(str(db) + ".refresh.lock", "a") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            job = active_job(db)
                            if job:
                                advance_job(db, job)
                except Exception:
                    log.exception("Automatic Power BI refresh worker failed; retrying")
                threading.Event().wait(10)

        thread = threading.Thread(target=work, daemon=True, name="powerbi-auto-refresh")
        _workers[key] = thread
        thread.start()
