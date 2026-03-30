import asyncio
import logging
import json
import os
import docker
from datetime import datetime, timedelta
from typing import Dict, Any, List
from app.config import settings, CONTAINER_MONITORING_FILE
from app.models import ContainerMonitoringConfig
from app.services.docker_service import get_logs, start_container
from app.services.notification_service import send_notification, generate_ai_notification

logger = logging.getLogger(__name__)

class MonitoringService:
    def __init__(self):
        self.client = docker.from_env()
        self.previous_states = {} # container_id -> status
        self.processed_logs = {}  # container_id -> timestamp of last processed log
        self.lock = asyncio.Lock()

    def get_monitoring_config(self, container_id_or_name: str) -> ContainerMonitoringConfig:
        config_data = settings.container_monitoring.get(container_id_or_name, {})
        if not config_data:
            return ContainerMonitoringConfig(enabled=False)
        return ContainerMonitoringConfig(**config_data)

    def save_monitoring_config(self, container_id_or_name: str, config: ContainerMonitoringConfig):
        settings.container_monitoring[container_id_or_name] = config.dict()
        os.makedirs(os.path.dirname(CONTAINER_MONITORING_FILE), exist_ok=True)
        CONTAINER_MONITORING_FILE.write_text(json.dumps(settings.container_monitoring, indent=2))

    async def check_containers(self, base_url: str):
        """
        Main polling loop.
        base_url: The current app base URL for action links.
        """
        try:
            containers = self.client.containers.list(all=True)
            for container in containers:
                config = self.get_monitoring_config(container.name)
                if not config.enabled:
                    # Update state anyway so we don't notify immediately when enabled
                    self.previous_states[container.id] = container.status
                    continue

                # 1. Check Status Change
                prev_status = self.previous_states.get(container.id)
                if prev_status == "running" and container.status != "running":
                    await self.handle_container_stopped(container, config, base_url)
                
                self.previous_states[container.id] = container.status

                # 2. Check Logs if running
                if config.enabled and config.monitor_logs and container.status == "running":
                    await self.check_container_logs(container, config, base_url)

        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")

    async def handle_container_stopped(self, container, config: ContainerMonitoringConfig, base_url: str):
        logger.info(f"Container {container.name} stopped. Autorestart: {config.auto_restart}")
        
        event_type = "Container Stopped"
        cause, effect, recommendation = await generate_ai_notification(event_type, container.name)
        
        links = [
            {"text": "Start Container", "url": f"{base_url}/action/start/{container.name}"},
            {"text": "View Dashboard", "url": base_url}
        ]
        
        await send_notification(
            title=f"ALERT: {container.name} is {container.status}",
            message_body=f"Container {container.name} transitioned from running to {container.status}.",
            cause=cause,
            effect=effect,
            recommendation=recommendation,
            links=links
        )
        
        if config.auto_restart:
            try:
                logger.info(f"Attempting auto-restart of {container.name}")
                container.start()
                await send_notification(
                    title=f"INFO: {container.name} Auto-Restarted",
                    message_body=f"Container {container.name} was automatically restarted by Monitoring Service.",
                    cause="Auto-restart enabled",
                    effect="System is back online",
                    recommendation="No further action needed"
                )
            except Exception as e:
                logger.error(f"Auto-restart failed for {container.name}: {e}")

    async def check_container_logs(self, container, config: ContainerMonitoringConfig, base_url: str):
        # Only check last 1 minute of logs
        since = datetime.now() - timedelta(minutes=1)
        try:
            logs = container.logs(since=int(since.timestamp()), tail=50).decode('utf-8')
            if not logs:
                return

            # Simple pattern matching
            found_patterns = []
            for pattern in config.log_patterns:
                if pattern.lower() in logs.lower():
                    found_patterns.append(pattern)
            
            if found_patterns:
                # To avoid spamming, we might want a cooldown per container
                # For now, just check if we already flagged something in the last 5 mins
                last_log_check = self.processed_logs.get(container.id)
                if last_log_check and (datetime.now() - last_log_check) < timedelta(minutes=5):
                    return
                
                self.processed_logs[container.id] = datetime.now()
                
                event_type = f"Critical Log Issue (pattern: {', '.join(found_patterns)})"
                cause, effect, recommendation = await generate_ai_notification(event_type, container.name, logs=logs)
                
                links = [
                    {"text": "Restart Container", "url": f"{base_url}/action/restart/{container.name}"},
                    {"text": "View Logs", "url": f"{base_url}?container={container.id}"}
                ]
                
                await send_notification(
                    title=f"WARNING: {container.name} Log Issues",
                    message_body=f"Found critical patterns in logs: {', '.join(found_patterns)}",
                    cause=cause,
                    effect=effect,
                    recommendation=recommendation,
                    links=links
                )
        except Exception as e:
            logger.debug(f"Could not fetch logs for {container.name}: {e}")

monitoring_service = MonitoringService()

async def monitoring_background_task(base_url: str):
    logger.info("Monitoring background task starting...")
    while True:
        await monitoring_service.check_containers(base_url)
        await asyncio.sleep(30) # Check every 30 seconds
