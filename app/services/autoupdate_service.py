"""
Watchtower-style auto-update service.

Periodically checks running containers for newer images and (optionally)
recreates them in place, preserving their configuration. Inspired by
https://github.com/nicholas-fedor/watchtower

Eligibility for a container is decided by combining:
  * the global scope ("all" vs "opt-in"),
  * the per-container monitoring config (``auto_update`` flag), and
  * Watchtower-compatible Docker labels (when ``respect_labels`` is on):
      - com.centurylinklabs.watchtower.enable        = "true" | "false"
      - com.centurylinklabs.watchtower.monitor-only  = "true"

A container is only ever *updated* when an actually newer image is found
(image-digest comparison), so enabling this is safe and idempotent.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import docker

from app.config import settings, AUTOUPDATE_CONFIG_FILE
from app.models import AutoUpdateSettings
from app.services import docker_service
from app.services.notification_service import send_notification

logger = logging.getLogger(__name__)

# Watchtower-compatible label keys
LABEL_ENABLE = "com.centurylinklabs.watchtower.enable"
LABEL_MONITOR_ONLY = "com.centurylinklabs.watchtower.monitor-only"


def get_settings() -> AutoUpdateSettings:
    return settings.autoupdate


def save_settings(config: AutoUpdateSettings) -> None:
    # Preserve last-run bookkeeping across user edits unless explicitly provided
    existing = settings.autoupdate
    if config.last_run is None:
        config.last_run = existing.last_run
    if config.last_summary is None:
        config.last_summary = existing.last_summary
    settings.autoupdate = config
    os.makedirs(os.path.dirname(AUTOUPDATE_CONFIG_FILE), exist_ok=True)
    AUTOUPDATE_CONFIG_FILE.write_text(json.dumps(config.dict(), indent=2))


def _persist_run_result(summary: str) -> None:
    settings.autoupdate.last_run = datetime.now(timezone.utc).isoformat()
    settings.autoupdate.last_summary = summary
    try:
        os.makedirs(os.path.dirname(AUTOUPDATE_CONFIG_FILE), exist_ok=True)
        AUTOUPDATE_CONFIG_FILE.write_text(json.dumps(settings.autoupdate.dict(), indent=2))
    except Exception as e:
        logger.warning(f"Could not persist auto-update run result: {e}")


def _label(container, key: str) -> str:
    # containers.list() exposes labels at attrs["Labels"]; a full inspect uses
    # attrs["Config"]["Labels"]. Check both so labels are honored either way.
    labels = (container.attrs.get("Config", {}) or {}).get("Labels") or {}
    if not labels:
        labels = container.attrs.get("Labels") or {}
    return (labels.get(key) or "").strip().lower()


def evaluate_container(container, config: AutoUpdateSettings):
    """
    Decide whether a container should be considered by the auto-updater.

    Returns (eligible: bool, monitor_only: bool).
    """
    # Never touch the ContainerManager app itself, or watchtower-disabled containers
    if config.respect_labels and _label(container, LABEL_ENABLE) == "false":
        return False, False

    # Per-container monitoring config (keyed by name, like the rest of the app)
    mon = settings.container_monitoring.get(container.name.lstrip("/"), {}) or {}
    per_container_optin = bool(mon.get("auto_update"))
    label_optin = config.respect_labels and _label(container, LABEL_ENABLE) == "true"

    if config.scope == "all":
        eligible = True
    else:  # "opt-in"
        eligible = per_container_optin or label_optin

    if not eligible:
        return False, False

    monitor_only = (
        config.monitor_only
        or bool(mon.get("auto_update_monitor_only"))
        or (config.respect_labels and _label(container, LABEL_MONITOR_ONLY) == "true")
    )
    return True, monitor_only


class AutoUpdateService:
    def __init__(self):
        self.client = docker.from_env()
        self.running = False  # guards against overlapping runs

    def _list_containers(self):
        return self.client.containers.list(all=False)  # only running containers

    async def run_cycle(self, manual: bool = False) -> dict:
        """
        Run one full auto-update pass. Returns a result dict with per-container
        outcomes and a summary string. Safe to call manually or on a schedule.
        """
        if self.running:
            return {"skipped": True, "message": "An auto-update run is already in progress."}

        self.running = True
        config = get_settings()
        results = []
        try:
            try:
                containers = await asyncio.to_thread(self._list_containers)
            except Exception as e:
                logger.error(f"Auto-update: could not list containers: {e}")
                return {"error": str(e), "results": [], "summary": "Failed to list containers."}

            for container in containers:
                name = container.name.lstrip("/")
                eligible, monitor_only = evaluate_container(container, config)
                if not eligible:
                    continue

                try:
                    if monitor_only:
                        check = await asyncio.to_thread(
                            docker_service.check_image_update, container.id, True
                        )
                        result = {
                            "name": name,
                            "checked": True,
                            "update_available": bool(check.get("available")),
                            "updated": False,
                            "monitor_only": True,
                            "image": check.get("image"),
                            "error": check.get("error"),
                        }
                        if check.get("available") and config.notify:
                            await self._notify_update_available(name, check.get("image"))
                    else:
                        upd = await asyncio.to_thread(
                            docker_service.auto_update_container, container.id, config.cleanup
                        )
                        result = {
                            "name": name,
                            "checked": True,
                            "update_available": bool(upd.get("updated")),
                            "updated": bool(upd.get("updated")),
                            "monitor_only": False,
                            "image": upd.get("image"),
                            "error": upd.get("error"),
                        }
                        if upd.get("updated") and config.notify:
                            await self._notify_updated(name, upd.get("image"), upd.get("new_id"))
                except Exception as e:
                    logger.error(f"Auto-update failed for {name}: {e}")
                    result = {"name": name, "checked": True, "updated": False,
                              "update_available": False, "error": str(e)}

                results.append(result)

            updated = [r for r in results if r.get("updated")]
            available = [r for r in results if r.get("update_available") and not r.get("updated")]
            errored = [r for r in results if r.get("error")]
            summary = (
                f"Checked {len(results)} container(s): "
                f"{len(updated)} updated, {len(available)} update(s) available (monitor-only), "
                f"{len(errored)} error(s)."
            )
            logger.info(f"Auto-update cycle complete. {summary}")
            _persist_run_result(summary)
            return {"results": results, "summary": summary, "checked": len(results),
                    "updated": len(updated), "available": len(available), "errors": len(errored)}
        finally:
            self.running = False

    async def _notify_updated(self, name: str, image: str, new_id: str):
        try:
            base_url = settings.APP_URL
            await send_notification(
                title=f"UPDATE: {name} auto-updated",
                message_body=f"Container '{name}' was updated to a newer image of '{image}' ({new_id}).",
                cause="A newer image was published to the registry.",
                effect="The container was recreated with the latest image, preserving its configuration.",
                recommendation="Verify the service is healthy after the update.",
                links=[{"text": "View Dashboard", "url": base_url}],
            )
        except Exception as e:
            logger.debug(f"Auto-update notification failed for {name}: {e}")

    async def _notify_update_available(self, name: str, image: str):
        try:
            base_url = settings.APP_URL
            await send_notification(
                title=f"UPDATE AVAILABLE: {name}",
                message_body=f"A newer image is available for '{name}' ({image}). "
                             f"Monitor-only mode is enabled, so it was not applied automatically.",
                cause="A newer image was published to the registry.",
                effect="The running container is still using the previous image.",
                recommendation="Update the container from the dashboard when ready.",
                links=[
                    {"text": "Update Container", "url": f"{base_url}?container={name}"},
                    {"text": "View Dashboard", "url": base_url},
                ],
            )
        except Exception as e:
            logger.debug(f"Auto-update availability notification failed for {name}: {e}")


autoupdate_service = AutoUpdateService()


async def autoupdate_background_task():
    """
    Background loop. Wakes up frequently to check whether a run is due, so that
    interval changes saved at runtime take effect without a restart.
    """
    logger.info("Auto-update background task starting...")
    last_run_monotonic = None
    loop = asyncio.get_event_loop()
    while True:
        try:
            config = get_settings()
            if config.enabled:
                now = loop.time()
                interval = max(60, int(config.interval_seconds or 86400))
                if last_run_monotonic is None or (now - last_run_monotonic) >= interval:
                    await autoupdate_service.run_cycle()
                    last_run_monotonic = loop.time()
        except Exception as e:
            logger.error(f"Auto-update background loop error: {e}")
        # Re-check the schedule every 30s (cheap; actual work is gated by interval)
        await asyncio.sleep(30)
