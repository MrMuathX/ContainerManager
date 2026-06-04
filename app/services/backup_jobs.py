import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.services import docker_service

BACKUP_DIR = Path(os.environ.get("CM_BACKUP_DIR", tempfile.gettempdir())) / "cm-backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def job_log(job_id: str, message: str, level: str = "info") -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["log_seq"] = job.get("log_seq", 0) + 1
        job["logs"].append({
            "seq": job["log_seq"],
            "ts": _now_iso(),
            "level": level,
            "message": message,
        })


def job_progress(job_id: str, pct: int, step: Optional[str] = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["progress"] = max(0, min(100, pct))
        if step is not None:
            job["step"] = step


def _make_progress_callback(job_id: str) -> Callable[[int, str], None]:
    def on_progress(pct: int, message: str) -> None:
        job_progress(job_id, pct, message)
        job_log(job_id, message)
    return on_progress


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
            "progress": 0,
            "step": "Starting backup",
            "logs": [],
            "log_seq": 0,
        }

    def worker() -> None:
        try:
            job_log(job_id, f"Starting backup for {container_name}")
            docker_service.create_container_backup_file(
                container_id,
                zip_path,
                on_progress=_make_progress_callback(job_id),
            )
            with _lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["path"] = zip_path
                _jobs[job_id]["progress"] = 100
                _jobs[job_id]["step"] = "Complete"
            job_log(job_id, "Backup ready for download", "done")
        except Exception as e:
            with _lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)
            job_log(job_id, str(e), "error")
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
