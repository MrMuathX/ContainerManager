from pydantic import BaseModel
from typing import Optional, Dict, List, Any, Union

class PortBinding(BaseModel):
    host_ip: str = ""
    host_port: str = ""
    container_port: str = ""

class ContainerSummary(BaseModel):
    id: str
    short_id: str
    name: str
    image: str
    image_id: str
    status: str          # running, exited, paused, etc.
    state: str           # running / stopped
    ports: List[PortBinding]
    created: str
    uptime: Optional[str] = None
    labels: Dict[str, str] = {}
    exit_code: int = 0

class ContainerStats(BaseModel):
    cpu_percent: float
    mem_usage_mb: float
    mem_limit_mb: float
    mem_percent: float
    net_rx_mb: float
    net_tx_mb: float
    block_read_mb: float
    block_write_mb: float

class ContainerDetail(BaseModel):
    id: str
    short_id: str
    name: str
    image: str
    image_id: str
    status: str
    state: str
    ports: List[PortBinding]
    created: str
    uptime: Optional[str] = None
    env: List[str]
    labels: Dict[str, str]
    mounts: List[Dict[str, Any]]
    network_mode: str
    networks: List[str]
    restart_policy: str
    command: Optional[str] = None
    stats: Optional[ContainerStats] = None

class SystemStats(BaseModel):
    cpu_percent: float
    cpu_count: int
    mem_used_gb: float
    mem_total_gb: float
    mem_percent: float
    disk_used_gb: float
    disk_total_gb: float
    disk_percent: float
    docker_containers_total: int
    docker_containers_running: int

class ActionResponse(BaseModel):
    success: bool
    message: str

class PullProgress(BaseModel):
    status: str
    progress: Optional[str] = None
    layer: Optional[str] = None

# --- Notification Models ---

class EmailConfig(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_email: str = ""
    to_email: str = ""
    use_tls: bool = True

class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: str = ""

class MQTTConfig(BaseModel):
    topic: str = "containermanager/notifications"

class WebhookConfig(BaseModel):
    url: str = ""
    method: str = "POST"
    headers: Dict[str, str] = {}

class NotificationSettings(BaseModel):
    email: EmailConfig = EmailConfig()
    telegram: TelegramConfig = TelegramConfig()
    mqtt: MQTTConfig = MQTTConfig()
    webhook: WebhookConfig = WebhookConfig()
    enabled_providers: List[str] = [] # "email", "telegram", "mqtt", "webhook"

class ContainerMonitoringConfig(BaseModel):
    enabled: bool = False
    auto_restart: bool = False
    auto_start_on_stop: bool = False
    monitor_logs: bool = False
    log_patterns: List[str] = ["error", "panic", "fatal", "exception"]
