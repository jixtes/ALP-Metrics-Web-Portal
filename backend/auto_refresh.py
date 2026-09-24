"""Durable, serialized refresh jobs. Shared by automatic and manual Settings refreshes."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time

from .database import connect_database
from powerbi.client import PowerBIClient, PowerBIConfig
from powerbi.fabric import FabricAPIError, FabricClient, validate_resource_id

REFRESH_SKU = "F32"

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
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER UNIQUE,
                status TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL);
        """)

        # Older deployments required a pipeline run for every refresh. Manual jobs
        # have no run_id; preserve existing jobs and their recovery state atomically.
        conn.execute("BEGIN IMMEDIATE")
        columns = conn.execute("PRAGMA table_info(powerbi_refresh_jobs)").fetchall()
        if any(row["name"] == "run_id" and row["notnull"] for row in columns):
            conn.execute("ALTER TABLE powerbi_refresh_jobs RENAME TO powerbi_refresh_jobs_old")
            conn.execute("""CREATE TABLE powerbi_refresh_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER UNIQUE,
                status TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL)""")
            conn.execute("INSERT INTO powerbi_refresh_jobs SELECT * FROM powerbi_refresh_jobs_old")
            conn.execute("DROP TABLE powerbi_refresh_jobs_old")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='powerbi_refresh_successes'").fetchone():
            conn.execute("""CREATE TABLE powerbi_refresh_successes (
                workspace TEXT NOT NULL, dataset TEXT NOT NULL, confirmed_at TEXT NOT NULL,
                PRIMARY KEY(workspace, dataset))""")
            # Preserve known successful timestamps from jobs recorded before this
            # table existed. Failed attempts never establish a successful refresh.
            for row in conn.execute("SELECT value FROM powerbi_refresh_jobs").fetchall():
                _store_successes(conn, json.loads(row["value"]))


def _store_successes(conn, value):
    workspace = value.get("config", {}).get("workspaceId")
    if not workspace:
        return
    for target in value.get("targets", []):
        timestamp = target.get("confirmedAt") or target.get("completedAt")
        if target.get("state") != "completed" or not timestamp:
            continue
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        except (TypeError, ValueError):
            continue
        conn.execute("""INSERT INTO powerbi_refresh_successes VALUES (?, ?, ?)
            ON CONFLICT(workspace, dataset) DO UPDATE SET confirmed_at=excluded.confirmed_at
            WHERE excluded.confirmed_at > powerbi_refresh_successes.confirmed_at""",
            (workspace, target["datasetId"], timestamp))


def successful_refreshes(db, workspace):
    with connect_database(db) as conn:
        rows = conn.execute("SELECT dataset, confirmed_at FROM powerbi_refresh_successes WHERE workspace=?", (workspace,)).fetchall()
    return {row["dataset"]: {"status": "Completed", "endTime": row["confirmed_at"]} for row in rows}


def _record_result(target, result):
    target["state"] = "completed" if result["status"] == "Completed" else "failed"
    if target["state"] == "completed":
        target["completedAt"] = result.get("endTime")
        # This is when the portal received confirmation, not an attempted refresh's
        # start/end time. Persist it even if F2 restoration is still in progress.
        target["confirmedAt"] = datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def settings(db):
    with connect_database(db) as conn:
        row = conn.execute("SELECT value FROM powerbi_auto_settings WHERE id=1").fetchone()
    value = json.loads(row[0]) if row else {"reportIds": [], "reports": []}
    value["capacityResourceId"] = value.get("capacityResourceId") or os.getenv("FABRIC_CAPACITY_RESOURCE_ID", "")
    return value


def save_settings(db, payload, client=None, fabric_factory=FabricClient):
    ids = payload.get("reportIds")
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ValueError("reportIds must be a list of dashboard IDs.")
    ids = list(dict.fromkeys(ids))
    resource = str(payload.get("capacityResourceId") or settings(db).get("capacityResourceId") or "").strip()
    value = {"reportIds": ids, "reports": [], "capacityResourceId": resource}
    if ids:
        if not resource:
            raise ValueError("Configure FABRIC_CAPACITY_RESOURCE_ID on the portal server before enabling automatic refresh.")
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


class RefreshBusy(ValueError):
    pass


def _require_idle(conn):
    if conn.execute("SELECT 1 FROM powerbi_refresh_jobs WHERE status NOT IN ('completed','failed','skipped') LIMIT 1").fetchone():
        raise RefreshBusy("Wait for the current dashboard refresh and F2 restoration to finish.")
    if conn.execute("""SELECT 1 FROM pipeline_runs WHERE status='running' AND id IN (
            SELECT MAX(id) FROM pipeline_runs WHERE extract_mode != 'surveycto_test'
            GROUP BY pipeline_version) LIMIT 1""").fetchone():
        raise RefreshBusy("Wait for the current data update to finish before refreshing a dashboard.")


def queue_manual_refresh(db, dataset_id, *, client=None, fabric_factory=FabricClient):
    # Check before remote reads, then repeat under the write lock to prevent a
    # concurrent manual refresh or pipeline reservation from slipping through.
    with connect_database(db) as conn:
        _require_idle(conn)
    resource = settings(db).get("capacityResourceId")
    if not resource:
        raise ValueError("Configure FABRIC_CAPACITY_RESOURCE_ID on the portal server before refreshing reports.")
    resource = validate_resource_id(resource)
    client = client or PowerBIClient(PowerBIConfig.from_env())
    reports = [{"id": r["id"], "name": r.get("name") or r["id"], "datasetId": dataset_id}
               for r in client.list_reports() if r.get("datasetId") == dataset_id]
    if not dataset_id or not reports:
        raise ValueError("Select a report with a semantic model from the configured workspace.")
    workspace = client.get_workspace()
    if not workspace.get("isOnDedicatedCapacity") or not workspace.get("capacityId"):
        raise ValueError("The Power BI workspace must be assigned to the configured Fabric capacity.")
    capacity = fabric_factory(resource).get()
    if capacity.get("sku", {}).get("name") != "F2" or capacity.get("properties", {}).get("state") != "Active":
        raise ValueError("The configured capacity must be active on F2 before refreshing reports.")
    config = {"capacityResourceId": resource, "workspaceId": client.config.workspace_id,
              "workspaceCapacityId": workspace["capacityId"], "reports": reports}
    value = {"trigger": "manual", "config": config,
             "targets": [{"datasetId": dataset_id, "names": [r["name"] for r in reports], "state": "pending"}],
             "created": time.time(), "stageStarted": time.time(), "ownsCapacity": False,
             "message": f"Dashboard refresh queued; Fabric capacity will scale to {REFRESH_SKU} and return to F2.",
             "error": "", "retryAt": 0}
    with connect_database(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_idle(conn)
        conn.execute("INSERT INTO powerbi_refresh_jobs (run_id,status,value,updated_at) VALUES (NULL,'queued',?,?)",
                     (json.dumps(value), time.time()))
    return latest_job(db)


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
            "trigger": value.get("trigger", "automatic"),
            "message": value["message"], "error": value.get("error", ""),
            "updatedAt": row["updated_at"], "active": row["status"] not in TERMINAL,
            "datasets": [{"datasetId": target["datasetId"],
                          "completedAt": (target.get("confirmedAt") or target.get("completedAt"))
                          if target.get("state") == "completed" else None}
                         for target in value["targets"]],
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
        _store_successes(conn, value)


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
    # Jobs already scaling/refreshing before the F32 rollout keep their original
    # F16 target so a deployment cannot strand their restoration or resize mid-run.
    boost_sku = value.get("boostSku", "F16")
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
                        _record_result(item, result)
                        continue
                    waiting = True
                    client.cancel_refresh(item["datasetId"], item["requestId"])
                except Exception:
                    waiting = True
            if not waiting or time.time() - value["cancelStarted"] >= 300:
                _restore(db, job, value, "Dashboard refresh exceeded the six-hour capacity boost limit; unfinished data remains pending.")
            else:
                value["message"] = "Capacity boost timed out; requesting refresh cancellation before restoring F2."
                _save(db, job, value)
            return

        if job["status"] == "queued":
            if value.get("trigger") != "manual":
                with connect_database(db) as conn:
                    run = conn.execute("SELECT status FROM pipeline_runs WHERE id=?", (job["run_id"],)).fetchone()
                if not run or run[0] == "running":
                    return
                if run[0] != "completed":
                    value["message"] = "Data update was not fully successful; automatic refresh skipped."
                    _save(db, job, value, "failed")
                    return
            if client.config.workspace_id != cfg["workspaceId"] or client.get_workspace().get("capacityId") != cfg["workspaceCapacityId"]:
                raise ValueError("Power BI workspace or capacity changed. Check the report and capacity settings before retrying.")
            actual = {r["id"]: r.get("datasetId") for r in client.list_reports()}
            if any(actual.get(r["id"]) != r["datasetId"] for r in cfg["reports"]):
                raise ValueError("A selected dashboard's semantic model changed. Check the report and capacity settings before retrying.")
            cap = fabric.get()
            if cap.get("sku", {}).get("name") != "F2" or cap.get("properties", {}).get("state") != "Active":
                raise ValueError("Dashboard refresh requires the capacity to start active on F2.")
            value["ownsCapacity"] = True
            value["boostSku"] = REFRESH_SKU
            value["boostStarted"] = time.time()
            value["message"] = f"Scaling Fabric capacity to {REFRESH_SKU}."
            _save(db, job, value, "scaling_up")
            # The next tick performs PATCH; a restart here resumes the recorded intent.
            return

        if job["status"] in {"scaling_up", "restoring"}:
            target = boost_sku if job["status"] == "scaling_up" else "F2"
            cap = fabric.get()
            sku = cap.get("sku", {}).get("name")
            value["lastCapacityState"] = {"sku": sku, "state": cap.get("properties", {}).get("state"),
                                          "provisioningState": cap.get("properties", {}).get("provisioningState")}
            ready = cap.get("properties", {}).get("state") == "Active" and cap.get("properties", {}).get("provisioningState") == "Succeeded"
            if sku == target and ready:
                if job["status"] == "scaling_up":
                    value["message"] = "Refreshing selected dashboards."
                    _save(db, job, value, "refreshing")
                else:
                    # A failed dataset stays pending even if other datasets completed.
                    with connect_database(db) as conn:
                        for item in value["targets"]:
                            if item["state"] == "completed" and value.get("trigger") != "manual":
                                conn.execute("INSERT OR REPLACE INTO powerbi_refresh_watermarks VALUES (?,?,?,?)",
                                             (cfg["workspaceId"], item["datasetId"], value["version"], value["fingerprint"]))
                        value["message"] = "Dashboard refresh failed; Fabric capacity restored to F2." if value["error"] else "Dashboards refreshed; Fabric capacity restored to F2."
                        job["status"] = "failed" if value["error"] else "completed"
                        conn.execute("UPDATE powerbi_refresh_jobs SET status=?,value=?,updated_at=? WHERE id=?",
                                     (job["status"], json.dumps(value), time.time(), job["id"]))
                return
            if job["status"] == "scaling_up" and time.time() - value["stageStarted"] > 900:
                detail = value.get("lastScaleError") or "Last capacity state: " + json.dumps(value["lastCapacityState"])
                _restore(db, job, value, f"Timed out while scaling to {boost_sku}. {detail}")
                return
            if sku not in {"F2", boost_sku}:
                raise RuntimeError(f"Capacity size changed outside this refresh. Waiting for F2 or {boost_sku} before continuing.")
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
                    _record_result(item, result)
                    if item["state"] == "failed":
                        value["error"] = "Refresh failed for " + ", ".join(item["names"]) + "."
                    _save(db, job, value)
                return
            _restore(db, job, value)
    except Exception as exc:
        value["retryAt"] = time.time() + 30
        if job["status"] == "scaling_up":
            value["lastScaleError"] = str(exc)
            # An invalid request, denied permission, or missing resource will not
            # recover by retrying the same scale-up for fifteen minutes. Still
            # verify/restore F2 before releasing the job's capacity reservation.
            if isinstance(exc, FabricAPIError) and exc.status_code in {400, 401, 403, 404, 422}:
                _restore(db, job, value, "Unable to scale Fabric capacity: " + str(exc))
                return
        if job["status"] == "queued":
            value["message"] = "Dashboard refresh could not start."
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
