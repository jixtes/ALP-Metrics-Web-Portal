#!/usr/bin/env python3
"""Run one scheduled V3 update, or validate the VM setup without running it."""
import argparse
import os
from pathlib import Path
import signal
import sys

from dotenv import load_dotenv

PORTAL_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PORTAL_ROOT / ".env")
sys.path.insert(0, str(PORTAL_ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check paths and imports without updating data or contacting external services.")
    args = parser.parse_args()
    os.chdir(PORTAL_ROOT)
    from backend.service import APP_DB_PATH
    from backend.pipelines.repository import pipeline_root
    from backend.scheduled_update import run_scheduled_update

    if not (PORTAL_ROOT / ".env").is_file():
        parser.error("Portal .env file was not found.")
    if not APP_DB_PATH.is_file():
        parser.error(f"Portal database was not found: {APP_DB_PATH}")
    if not (pipeline_root("V3") / ".git").exists():
        parser.error(f"V3 repository was not found: {pipeline_root('V3')}")
    if args.check:
        print(f"V3 scheduler ready. Python: {sys.executable}")
        print(f"Portal database: {APP_DB_PATH}")
        print(f"V3 repository: {pipeline_root('V3')}")
        print("Check only: no data update, upload, or Power BI refresh was started.")
        return 0

    def terminate(signum, frame):
        raise InterruptedError("Scheduled update terminated by systemd.")
    signal.signal(signal.SIGTERM, terminate)
    return run_scheduled_update(APP_DB_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
