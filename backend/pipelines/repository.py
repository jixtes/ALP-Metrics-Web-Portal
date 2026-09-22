"""Repository configuration shared by the separate V2 and V3 adapters."""
import os
from pathlib import Path
import subprocess
from threading import Lock

PORTAL_ROOT = Path(__file__).resolve().parents[2]
# V3 changes the process working directory, so runs and pulls are serialized.
RUN_LOCK = Lock()


def pipeline_root(version: str) -> Path:
    setting, default = (("ALP_V2_PIPELINE_REPO_PATH", "../alp-metrics-pipeline-v2")
                        if version == "V2" else ("ALP_PIPELINE_REPO_PATH", "../alp-metrics-pipeline"))
    path = Path(os.getenv(setting) or default).expanduser()
    return (path if path.is_absolute() else PORTAL_ROOT / path).resolve()


def git_output(root: Path, *args: str) -> str:
    if not root.is_dir():
        return ""
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                            timeout=30, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def repo_status(root: Path, version: str) -> dict:
    dirty = git_output(root, "status", "--porcelain")
    return {"pipeline_version": version, "root": str(root),
            "branch": git_output(root, "rev-parse", "--abbrev-ref", "HEAD"),
            "commit": git_output(root, "rev-parse", "HEAD"),
            "upstream": git_output(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
            "isDirty": bool(dirty), "dirtyFiles": dirty.splitlines()}


def commit_details(root: Path, commit: str | None) -> dict:
    if not commit:
        return {}
    return {"pipeline_commit_subject": git_output(root, "show", "-s", "--format=%s", commit),
            "pipeline_commit_at": git_output(root, "show", "-s", "--format=%cI", commit),
            "pipeline_commit_author": git_output(root, "show", "-s", "--format=%an", commit)}


def pull_repo(root: Path, version: str) -> dict:
    with RUN_LOCK:
        before = repo_status(root, version)
        if before["isDirty"] or not before["branch"]:
            return {"status": "blocked", "before": before, "after": before,
                    "output": "Repository is missing or has local changes. Commit changes before pulling."}
        result = subprocess.run(["git", "pull", "--ff-only", "origin", before["branch"]],
                                cwd=root, text=True, capture_output=True, timeout=300, check=False)
        return {"status": "completed" if result.returncode == 0 else "failed",
                "returnCode": result.returncode, "before": before, "after": repo_status(root, version),
                "output": "\n".join(part.strip() for part in [result.stdout, result.stderr] if part.strip())}
