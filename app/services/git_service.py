"""
Coolify-style Git deployments.

Point the app at a GitHub repository; it builds a Docker image from the repo's
Dockerfile (the Docker daemon clones the remote git context) and (re)deploys it
as a container. A GitHub push webhook triggers an automatic redeploy.

Inspired by https://github.com/coollabsio/coolify
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Callable
from urllib.parse import urlparse, urlunparse

import docker

from app.config import settings, GIT_APPS_FILE
from app.models import GitApp
from app.services import docker_service

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}  # deploy job registry (in-memory, streamed to UI)

# Label applied to every container we deploy, so they can be identified later
LABEL_GIT_APP = "com.containermanager.git-app"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client() -> docker.DockerClient:
    return docker_service._get_client()


# ── Persistence / CRUD ────────────────────────────────────────────────────────

def _save_all() -> None:
    os.makedirs(os.path.dirname(GIT_APPS_FILE), exist_ok=True)
    GIT_APPS_FILE.write_text(json.dumps(settings.git_apps, indent=2))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()).strip("-")
    return slug or f"app-{uuid.uuid4().hex[:8]}"


def list_apps() -> list[dict]:
    return list(settings.git_apps.values())


def get_app(app_id: str) -> Optional[dict]:
    return settings.git_apps.get(app_id)


def _derive_name_from_repo(repo_url: str) -> str:
    path = urlparse(repo_url).path.strip("/")
    name = path.split("/")[-1] if path else "app"
    return name[:-4] if name.endswith(".git") else name


def create_app(data: GitApp) -> dict:
    app = data.dict()

    if not app.get("name"):
        app["name"] = _derive_name_from_repo(app["repo_url"])

    base_id = slugify(app.get("id") or app["name"])
    app_id = base_id
    suffix = 1
    while app_id in settings.git_apps:
        suffix += 1
        app_id = f"{base_id}-{suffix}"
    app["id"] = app_id

    if not app.get("container_name"):
        app["container_name"] = app_id
    if not app.get("image_name"):
        app["image_name"] = f"cm-git/{app_id}:latest"
    if not app.get("webhook_secret"):
        app["webhook_secret"] = secrets.token_hex(20)
    app["created_at"] = _now_iso()
    app["last_deploy"] = None

    settings.git_apps[app_id] = app
    _save_all()
    return app


def update_app(app_id: str, data: GitApp) -> Optional[dict]:
    existing = settings.git_apps.get(app_id)
    if not existing:
        return None
    incoming = data.dict()
    # Preserve server-managed fields
    incoming["id"] = app_id
    incoming["created_at"] = existing.get("created_at")
    incoming["last_deploy"] = existing.get("last_deploy")
    incoming["webhook_secret"] = existing.get("webhook_secret") or secrets.token_hex(20)
    incoming["image_name"] = existing.get("image_name") or f"cm-git/{app_id}:latest"
    if not incoming.get("container_name"):
        incoming["container_name"] = existing.get("container_name") or app_id
    if not incoming.get("name"):
        incoming["name"] = existing.get("name") or _derive_name_from_repo(incoming["repo_url"])
    settings.git_apps[app_id] = incoming
    _save_all()
    return incoming


def regenerate_secret(app_id: str) -> Optional[str]:
    app = settings.git_apps.get(app_id)
    if not app:
        return None
    app["webhook_secret"] = secrets.token_hex(20)
    _save_all()
    return app["webhook_secret"]


def delete_app(app_id: str, remove_container: bool = True) -> bool:
    app = settings.git_apps.pop(app_id, None)
    if app is None:
        return False
    _save_all()
    if remove_container:
        try:
            c = _client().containers.get(app["container_name"])
            c.remove(force=True)
        except Exception:
            pass
    return True


# ── Build context / remote ref ────────────────────────────────────────────────

def _build_remote_ref(app: dict) -> str:
    """
    Construct the remote git build context understood by the Docker daemon:
        https://github.com/user/repo.git#<branch>:<context>
    A private-repo token (if set) is injected into the URL userinfo.
    """
    repo_url = (app.get("repo_url") or "").strip()
    parsed = urlparse(repo_url)
    netloc = parsed.netloc

    token = (app.get("private_token") or "").strip()
    if token:
        # Strip any existing userinfo, then inject the token
        host = netloc.split("@")[-1]
        netloc = f"{token}@{host}"

    path = parsed.path
    if not path.endswith(".git"):
        path = path + ".git"

    base = urlunparse((parsed.scheme or "https", netloc, path, "", "", ""))

    branch = (app.get("branch") or "main").strip()
    context = (app.get("build_context") or ".").strip().strip("/")
    fragment = branch
    if context and context != ".":
        fragment = f"{branch}:{context}"
    return f"{base}#{fragment}"


def _redact(remote_ref: str) -> str:
    """Hide any injected token when echoing the remote ref into logs."""
    return re.sub(r"//[^@/]+@", "//***@", remote_ref)


# ── Deploy job machinery ──────────────────────────────────────────────────────

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


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _cleanup_old_jobs(max_age_sec: int = 3600) -> None:
    now = time.time()
    with _lock:
        stale = [jid for jid, j in _jobs.items()
                 if now - j.get("created", now) > max_age_sec and j.get("status") != "running"]
        for jid in stale:
            _jobs.pop(jid, None)


def _record_deploy_result(app_id: str, status: str, message: str, commit: Optional[str] = None) -> None:
    app = settings.git_apps.get(app_id)
    if not app:
        return
    app["last_deploy"] = {
        "status": status,
        "message": message,
        "commit": commit,
        "timestamp": _now_iso(),
    }
    _save_all()


def _do_build_and_deploy(app: dict, on_log: Callable[[str, str], None], commit: Optional[str]) -> dict:
    """
    Build the image from the repo and (re)create the container.
    on_log(message, level) receives progress. Returns a result dict.
    Runs synchronously (call from a worker thread).
    """
    client = _client()
    image_name = app["image_name"]
    container_name = app["container_name"]
    remote_ref = _build_remote_ref(app)

    on_log(f"Building {image_name} from {_redact(remote_ref)}", "info")
    on_log(f"Dockerfile: {app.get('dockerfile', 'Dockerfile')}", "info")

    # 1. Build the image from the remote git context (daemon clones the repo)
    try:
        build_stream = client.api.build(
            path=remote_ref,
            dockerfile=app.get("dockerfile") or "Dockerfile",
            tag=image_name,
            rm=True,
            pull=True,
            forcerm=True,
            decode=True,
        )
        for chunk in build_stream:
            if "stream" in chunk:
                text = chunk["stream"].rstrip()
                if text:
                    on_log(text, "build")
            elif "error" in chunk:
                raise RuntimeError(chunk["error"])
            elif "status" in chunk:
                status = chunk.get("status", "")
                progress = chunk.get("progress", "")
                line = f"{status} {progress}".strip()
                if line:
                    on_log(line, "build")
    except docker.errors.BuildError as e:
        raise RuntimeError(f"Build failed: {e}")

    on_log("Image built successfully", "info")

    # 2. Replace the old container (if any), preserving the configured runtime
    try:
        old = client.containers.get(container_name)
        on_log(f"Stopping existing container {container_name}", "info")
        old.stop(timeout=10)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass

    # Resolve ports: {"8080/tcp": "8080"} -> docker-py {"8080/tcp": "8080"}
    raw_ports = {}
    for cport, hport in (app.get("ports") or {}).items():
        key = cport if "/" in cport else f"{cport}/tcp"
        raw_ports[key] = [("0.0.0.0", str(hport))] if hport else [("0.0.0.0", "")]
    resolved_ports = docker_service.resolve_port_conflicts(client, raw_ports) if raw_ports else {}

    labels = {LABEL_GIT_APP: app["id"]}

    on_log(f"Starting container {container_name}", "info")
    new_container = client.containers.run(
        image_name,
        name=container_name,
        detach=True,
        environment=app.get("env") or [],
        ports=resolved_ports,
        volumes=app.get("volumes") or None,
        restart_policy={"Name": app.get("restart_policy") or "unless-stopped"},
        network_mode=app.get("network_mode") or "bridge",
        labels=labels,
    )
    on_log(f"Deployed {container_name} ({new_container.short_id})", "done")
    return {"container_id": new_container.id, "short_id": new_container.short_id}


def start_deploy(app_id: str, commit: Optional[str] = None, trigger: str = "manual") -> Optional[str]:
    """Kick off a deploy in a background thread. Returns a job_id, or None if unknown app."""
    app = settings.git_apps.get(app_id)
    if not app:
        return None

    _cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "app_id": app_id,
            "status": "running",
            "trigger": trigger,
            "commit": commit,
            "error": None,
            "created": time.time(),
            "logs": [],
            "log_seq": 0,
        }

    def on_log(message: str, level: str = "info") -> None:
        job_log(job_id, message, level)

    def worker() -> None:
        try:
            on_log(f"Deploy started ({trigger})", "info")
            result = _do_build_and_deploy(app, on_log, commit)
            with _lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = result
            _record_deploy_result(app_id, "success", "Deployed successfully", commit)
        except Exception as e:
            with _lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)
            on_log(str(e), "error")
            _record_deploy_result(app_id, "failed", str(e), commit)

    threading.Thread(target=worker, daemon=True, name=f"cm-deploy-{job_id[:8]}").start()
    return job_id


# ── Webhook verification ──────────────────────────────────────────────────────

def verify_signature(secret: str, payload: bytes, signature_header: Optional[str]) -> bool:
    """Verify a GitHub webhook 'X-Hub-Signature-256' header against the raw body."""
    if not signature_header or not secret:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def ref_matches_branch(ref: Optional[str], branch: str) -> bool:
    """A GitHub push 'ref' looks like 'refs/heads/main'."""
    if not ref:
        return False
    return ref == f"refs/heads/{branch}" or ref.split("/")[-1] == branch
