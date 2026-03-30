import asyncio
import json
import logging
from typing import Optional

try:
    import aiomqtt
    AIOMQTT_AVAILABLE = True
except ImportError:
    AIOMQTT_AVAILABLE = False

from app.config import settings

logger = logging.getLogger(__name__)

# Global state shared between routers and MQTT background task
_container_states: dict[str, str] = {}  # name -> "ON" | "OFF"


def update_state(container_name: str, state: str):
    """Called by the container router when state changes."""
    _container_states[container_name] = state


def _discovery_payload(name: str) -> dict:
    safe = name.replace("-", "_").replace(" ", "_")
    return {
        "name": f"Container {name}",
        "unique_id": f"cm_container_{safe}",
        "state_topic": f"{settings.HA_DISCOVERY_PREFIX}/switch/container_{safe}/state",
        "command_topic": f"{settings.HA_DISCOVERY_PREFIX}/switch/container_{safe}/set",
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "icon": "mdi:docker",
        "device": {
            "identifiers": ["containermanager"],
            "name": "ContainerManager",
            "model": "FastAPI Docker Manager",
            "manufacturer": "ContainerManager",
        },
    }


async def mqtt_background_task(get_containers_fn):
    """
    Long-running background task that:
    1. Publishes HA discovery for each container
    2. Publishes live state updates every STATS_INTERVAL seconds
    3. Listens for command messages to start/stop/restart containers
    """
    if not AIOMQTT_AVAILABLE:
        logger.warning("aiomqtt not installed; MQTT disabled.")
        return

    from app.services import docker_service

    logger.info(f"Starting MQTT background task → {settings.MQTT_HOST}:{settings.MQTT_PORT}")

    reconnect_interval = 5
    while True:
        try:
            async with aiomqtt.Client(
                hostname=settings.MQTT_HOST,
                port=settings.MQTT_PORT,
                username=settings.MQTT_USER or None,
                password=settings.MQTT_PASSWORD or None,
                identifier=settings.MQTT_CLIENT_ID,
            ) as client:
                logger.info("MQTT connected.")

                # Initial discovery + subscribe
                containers = get_containers_fn()
                for c in containers:
                    safe = c.name.replace("-", "_").replace(" ", "_")
                    disc_topic = f"{settings.HA_DISCOVERY_PREFIX}/switch/container_{safe}/config"
                    cmd_topic  = f"{settings.HA_DISCOVERY_PREFIX}/switch/container_{safe}/set"
                    await client.publish(disc_topic, json.dumps(_discovery_payload(c.name)), retain=True)
                    await client.subscribe(cmd_topic)

                # Publish initial states
                for c in containers:
                    safe = c.name.replace("-", "_").replace(" ", "_")
                    state_topic = f"{settings.HA_DISCOVERY_PREFIX}/switch/container_{safe}/state"
                    state = "ON" if c.state == "running" else "OFF"
                    await client.publish(state_topic, state, retain=True)
                    _container_states[c.name] = state

                # Listen for commands and periodically publish states
                async def listen():
                    async for message in client.messages:
                        topic = str(message.topic)
                        payload = message.payload.decode()
                        # Extract container name from topic
                        # topic: homeassistant/switch/container_{safe}/set
                        parts = topic.split("/")
                        if len(parts) >= 3:
                            safe_name = parts[2].replace("container_", "", 1)
                            # Find real container by matching safe name
                            try:
                                all_c = docker_service.list_containers()
                                matched = next(
                                    (c for c in all_c if c.name.replace("-", "_").replace(" ", "_") == safe_name),
                                    None
                                )
                                if matched:
                                    if payload == "ON":
                                        docker_service.start_container(matched.id)
                                    elif payload == "OFF":
                                        docker_service.stop_container(matched.id)
                                    elif payload == "RESTART":
                                        docker_service.restart_container(matched.id)
                            except Exception as e:
                                logger.error(f"MQTT command error: {e}")

                async def publish_loop():
                    while True:
                        await asyncio.sleep(settings.STATS_INTERVAL)
                        try:
                            all_c = docker_service.list_containers()
                            # Publish discovery for any new containers
                            for c in all_c:
                                safe = c.name.replace("-", "_").replace(" ", "_")
                                if c.name not in _container_states:
                                    disc_topic = f"{settings.HA_DISCOVERY_PREFIX}/switch/container_{safe}/config"
                                    cmd_topic  = f"{settings.HA_DISCOVERY_PREFIX}/switch/container_{safe}/set"
                                    await client.publish(disc_topic, json.dumps(_discovery_payload(c.name)), retain=True)
                                    await client.subscribe(cmd_topic)

                                state = "ON" if c.state == "running" else "OFF"
                                state_topic = f"{settings.HA_DISCOVERY_PREFIX}/switch/container_{safe}/state"
                                await client.publish(state_topic, state, retain=True)
                                _container_states[c.name] = state
                        except Exception as e:
                            logger.error(f"MQTT publish loop error: {e}")

                await asyncio.gather(listen(), publish_loop())

        except Exception as e:
            logger.error(f"MQTT error: {e}. Reconnecting in {reconnect_interval}s...")
            await asyncio.sleep(reconnect_interval)
