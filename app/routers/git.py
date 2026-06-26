import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.models import GitApp
from app.services import git_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/git", tags=["git"])


def _public_view(app: dict) -> dict:
    """App representation for the UI (secret/token are masked)."""
    view = dict(app)
    view["has_private_token"] = bool(app.get("private_token"))
    view.pop("private_token", None)
    return view


@router.get("/apps")
def list_git_apps():
    return [_public_view(a) for a in git_service.list_apps()]


@router.get("/apps/{app_id}")
def get_git_app(app_id: str):
    app = git_service.get_app(app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Git app not found")
    return _public_view(app)


@router.post("/apps")
def create_git_app(payload: GitApp):
    if not payload.repo_url.strip():
        raise HTTPException(status_code=400, detail="repo_url is required")
    app = git_service.create_app(payload)
    return _public_view(app)


@router.put("/apps/{app_id}")
def update_git_app(app_id: str, payload: GitApp):
    # An empty private_token in the payload means "keep existing"
    existing = git_service.get_app(app_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Git app not found")
    if not payload.private_token:
        payload.private_token = existing.get("private_token", "")
    updated = git_service.update_app(app_id, payload)
    return _public_view(updated)


@router.delete("/apps/{app_id}")
def delete_git_app(app_id: str, remove_container: bool = True):
    ok = git_service.delete_app(app_id, remove_container=remove_container)
    if not ok:
        raise HTTPException(status_code=404, detail="Git app not found")
    return {"status": "success", "message": "Git app deleted"}


@router.post("/apps/{app_id}/regenerate-secret")
def regenerate_webhook_secret(app_id: str):
    secret = git_service.regenerate_secret(app_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Git app not found")
    return {"webhook_secret": secret}


@router.post("/apps/{app_id}/deploy")
def deploy_git_app(app_id: str):
    job_id = git_service.start_deploy(app_id, trigger="manual")
    if job_id is None:
        raise HTTPException(status_code=404, detail="Git app not found")
    return {"job_id": job_id, "status": "running"}


@router.get("/apps/{app_id}/deploy/{job_id}")
def get_deploy_status(app_id: str, job_id: str):
    job = git_service.get_job(job_id)
    if not job or job.get("app_id") != app_id:
        raise HTTPException(status_code=404, detail="Deploy job not found")
    return {
        "status": job["status"],
        "error": job.get("error"),
        "trigger": job.get("trigger"),
        "logs": job.get("logs", []),
        "log_seq": job.get("log_seq", 0),
    }


@router.get("/apps/{app_id}/webhook-info")
def webhook_info(app_id: str, request: Request):
    """Return the webhook URL + secret to paste into GitHub repo settings."""
    app = git_service.get_app(app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Git app not found")
    base = str(request.base_url).rstrip("/")
    return {
        "url": f"{base}/webhook/git/{app_id}",
        "secret": app.get("webhook_secret", ""),
        "content_type": "application/json",
        "events": "Just the push event",
    }


# ── GitHub push webhook (NO auth cookie; verified via HMAC) ────────────────────
# Mounted at the app root in main.py so it is exempt from the dashboard auth
# middleware. GitHub cannot present a session cookie.

webhook_router = APIRouter(tags=["git-webhook"])


@webhook_router.post("/webhook/git/{app_id}")
async def github_webhook(app_id: str, request: Request):
    app = git_service.get_app(app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Git app not found")

    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    event = request.headers.get("X-GitHub-Event", "")

    if not git_service.verify_signature(app.get("webhook_secret", ""), raw, signature):
        logger.warning(f"Webhook signature verification failed for git app '{app_id}'")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # GitHub sends a 'ping' when the webhook is first created
    if event == "ping":
        return {"status": "ok", "message": "pong"}

    if event != "push":
        return {"status": "ignored", "message": f"Ignoring '{event}' event"}

    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    ref = body.get("ref")
    if not git_service.ref_matches_branch(ref, app.get("branch", "main")):
        return {"status": "ignored", "message": f"Push to {ref} does not match branch {app.get('branch')}"}

    if not app.get("auto_deploy", True):
        return {"status": "skipped", "message": "Auto-deploy is disabled for this app"}

    commit = (body.get("after") or "")[:12] or None
    job_id = git_service.start_deploy(app_id, commit=commit, trigger="webhook")
    logger.info(f"Webhook triggered deploy for git app '{app_id}' (commit {commit}, job {job_id})")
    return {"status": "deploying", "job_id": job_id, "commit": commit}
