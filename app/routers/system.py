import psutil
import docker
from typing import List
from fastapi import APIRouter
from pydantic import BaseModel
from app.models import SystemStats, NotificationSettings

router = APIRouter(prefix="/api/system", tags=["system"])

class PortInfo(BaseModel):
    port: str
    protocol: str
    container_name: str
    container_id: str

@router.get("", response_model=SystemStats)
def get_system_stats():
    cpu = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count(logical=True)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    try:
        client = docker.from_env()
        all_c = client.containers.list(all=True)
        running_c = [c for c in all_c if c.status == "running"]
        total = len(all_c)
        running = len(running_c)
    except Exception:
        total = running = 0

    return SystemStats(
        cpu_percent=cpu,
        cpu_count=cpu_count,
        mem_used_gb=round(mem.used / 1024**3, 2),
        mem_total_gb=round(mem.total / 1024**3, 2),
        mem_percent=mem.percent,
        disk_used_gb=round(disk.used / 1024**3, 2),
        disk_total_gb=round(disk.total / 1024**3, 2),
        disk_percent=disk.percent,
        docker_containers_total=total,
        docker_containers_running=running,
    )

@router.get("/ports", response_model=List[PortInfo])
def get_system_ports():
    client = docker.from_env()
    ports_list = []
    try:
        containers = client.containers.list(all=True)
        for c in containers:
            if c.status == "running":
                bindings = c.ports or {}
                for container_port, host_bindings in bindings.items():
                    if host_bindings:
                        proto = "tcp"
                        c_port_str = container_port
                        if "/" in container_port:
                            c_port_str, proto = container_port.split("/")
                        
                        for hb in host_bindings:
                            h_port = hb.get("HostPort")
                            if h_port:
                                ports_list.append(PortInfo(
                                    port=h_port,
                                    protocol=proto.upper(),
                                    container_name=c.name.lstrip("/"),
                                    container_id=c.short_id
                                ))
    except Exception:
        pass
    
    ports_list.sort(key=lambda x: int(x.port) if x.port.isdigit() else 0)
    return ports_list

@router.get("/mqtt-status")
def get_mqtt_status():
    from app.services.ha_mqtt import AIOMQTT_AVAILABLE
    from app.config import settings
    return {
        "enabled": settings.MQTT_ENABLED,
        "available": AIOMQTT_AVAILABLE,
        "host": settings.MQTT_HOST
    }

class SystemConfig(BaseModel):
    dashboard_password: str = ""
    app_url: str = "http://localhost:5000"
    enabled: bool
    host: str
    port: int
    user: str
    password: str
    client_id: str
    discovery_prefix: str

@router.get("/system-config")
def get_system_config():
    from app.config import settings
    return {
        "dashboard_password": settings.DASHBOARD_PASSWORD,
        "app_url": settings.APP_URL,
        "enabled": settings.MQTT_ENABLED,
        "host": settings.MQTT_HOST,
        "port": settings.MQTT_PORT,
        "user": settings.MQTT_USER,
        "password": settings.MQTT_PASSWORD,
        "client_id": settings.MQTT_CLIENT_ID,
        "discovery_prefix": settings.HA_DISCOVERY_PREFIX
    }

@router.get("/notifications", response_model=NotificationSettings)
def get_notification_settings():
    from app.config import settings
    return settings.notification_settings

@router.post("/notifications")
def update_notification_settings(config: NotificationSettings):
    import json
    import os
    from app.config import settings, NOTIFICATION_SETTINGS_FILE
    
    settings.notification_settings = config
    os.makedirs(os.path.dirname(NOTIFICATION_SETTINGS_FILE), exist_ok=True)
    NOTIFICATION_SETTINGS_FILE.write_text(config.json(indent=2))
    
    return {"status": "success", "message": "Notification settings updated"}

@router.post("/system-config")
def update_system_config(config: SystemConfig):
    import json
    import os
    from app.config import SYSTEM_CONFIG_FILE
    
    data = {
        "DASHBOARD_PASSWORD": config.dashboard_password,
        "APP_URL": config.app_url,
        "MQTT_ENABLED": config.enabled,
        "MQTT_HOST": config.host,
        "MQTT_PORT": config.port,
        "MQTT_USER": config.user,
        "MQTT_PASSWORD": config.password,
        "MQTT_CLIENT_ID": config.client_id,
        "HA_DISCOVERY_PREFIX": config.discovery_prefix
    }
    
    os.makedirs(os.path.dirname(SYSTEM_CONFIG_FILE), exist_ok=True)
    SYSTEM_CONFIG_FILE.write_text(json.dumps(data, indent=2))
    
    # We exit the process to let Docker restart it, ensuring the new config is applied to background tasks
    import threading
    def delay_exit():
        os._exit(0)
    threading.Timer(0.5, delay_exit).start()
    
    return {"status": "success", "message": "Restarting container to apply changes..."}

@router.post("/mqtt-test")
async def test_mqtt_config(config: SystemConfig):
    from app.services.ha_mqtt import AIOMQTT_AVAILABLE
    if not AIOMQTT_AVAILABLE:
        raise HTTPException(status_code=400, detail="aiomqtt library not installed")
    import aiomqtt
    try:
        async with aiomqtt.Client(
            hostname=config.host,
            port=config.port,
            username=config.user or None,
            password=config.password or None,
            identifier=config.client_id + "_test",
        ) as client:
            return {"status": "success", "message": "Connection to MQTT broker successful!"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")
@router.post("/notifications/test")
async def test_notification(provider: str, config: NotificationSettings):
    from app.services.notification_service import test_notification_provider
    result = await test_notification_provider(provider, config)
    if not result["success"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=result["message"])
    return result
