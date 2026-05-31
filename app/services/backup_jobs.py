import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.services import docker_service

BACKUP_DIR = Path(os.environ.get("CM_BACKUP_DIR", tempfile.gettempdir())) / "cm-backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _cleanup_old_jobs(max_age_sec: int = 3600) -> None:
    now = time.time()
    with _lock:
        stale = [
            job_id
            for job_id, job in _jobs.items()
            if now - job.get("created", now) > max_age_sec
        ]
        for job_id in stale:
            job = _jobs.pop(job_id, None)
            if job and job.get("path"):
                try:
                    os.unlink(job["path"])
                except OSError:
                    pass


def start_container_backup_job(container_id: str, container_name: str) -> str:
    _cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    zip_path = str(BACKUP_DIR / f"{job_id}.zip")
    with _lock:
        _jobs[job_id] = {
            "status": "running",
            "error": None,
            "filename": f"{container_name}_backup.zip",
            "path": None,
            "created": time.time(),
        }

    def worker() -> None:
        try:
            docker_service.create_container_backup_file(container_id, zip_path)
            with _lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["path"] = zip_path
        except Exception as e:
            with _lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)
            try:
                if os.path.exists(zip_path):
                    os.unlink(zip_path)
            except OSError:
                pass

    threading.Thread(target=worker, daemon=True, name=f"cm-backup-{job_id[:8]}").start()
    return job_id


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def release_job(job_id: str) -> None:
    with _lock:
        job = _jobs.pop(job_id, None)
    if job and job.get("path"):
        try:
            os.unlink(job["path"])
        except OSError:
            pass
