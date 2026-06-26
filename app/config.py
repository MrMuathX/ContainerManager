import os
import json
from pathlib import Path

SYSTEM_CONFIG_FILE = Path("data/system_config.json")
NOTIFICATION_SETTINGS_FILE = Path("data/notification_settings.json")
CONTAINER_MONITORING_FILE = Path("data/container_monitoring.json")
AUTOUPDATE_CONFIG_FILE = Path("data/autoupdate_config.json")
GIT_APPS_FILE = Path("data/git_apps.json")

class Settings:
    def __init__(self):
        self._load_system_config()

    def _load_system_config(self):
        # Default from env
        self.DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "admin")
        self.MQTT_ENABLED = os.getenv("MQTT_ENABLED", "false").lower() == "true"
        self.MQTT_HOST = os.getenv("MQTT_HOST", "homeassistant.local")
        self.MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
        self.MQTT_USER = os.getenv("MQTT_USER", "")
        self.MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
        self.MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "containermanager")
        self.HA_DISCOVERY_PREFIX = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")
        self.STATS_INTERVAL = int(os.getenv("STATS_INTERVAL", "5"))
        self.APP_URL = os.getenv("APP_URL", "http://localhost:5000")

        # Override from json
        if SYSTEM_CONFIG_FILE.exists():
            try:
                data = json.loads(SYSTEM_CONFIG_FILE.read_text())
                if "DASHBOARD_PASSWORD" in data and data["DASHBOARD_PASSWORD"]: 
                    self.DASHBOARD_PASSWORD = data["DASHBOARD_PASSWORD"]
                if "MQTT_ENABLED" in data: self.MQTT_ENABLED = data["MQTT_ENABLED"]
                if "MQTT_HOST" in data: self.MQTT_HOST = data["MQTT_HOST"]
                if "MQTT_PORT" in data: self.MQTT_PORT = int(data["MQTT_PORT"])
                if "MQTT_USER" in data: self.MQTT_USER = data["MQTT_USER"]
                if "MQTT_PASSWORD" in data: self.MQTT_PASSWORD = data["MQTT_PASSWORD"]
                if "MQTT_CLIENT_ID" in data: self.MQTT_CLIENT_ID = data["MQTT_CLIENT_ID"]
                if "HA_DISCOVERY_PREFIX" in data: self.HA_DISCOVERY_PREFIX = data["HA_DISCOVERY_PREFIX"]
                if "APP_URL" in data: self.APP_URL = data["APP_URL"]
            except Exception as e:
                print(f"Error loading system_config.json: {e}")

        # Load notifications
        from app.models import NotificationSettings
        self.notification_settings = NotificationSettings()
        if NOTIFICATION_SETTINGS_FILE.exists():
            try:
                data = json.loads(NOTIFICATION_SETTINGS_FILE.read_text())
                self.notification_settings = NotificationSettings(**data)
            except Exception as e:
                print(f"Error loading notification_settings.json: {e}")

        # Load container monitoring config
        self.container_monitoring = {}
        if CONTAINER_MONITORING_FILE.exists():
            try:
                self.container_monitoring = json.loads(CONTAINER_MONITORING_FILE.read_text())
            except Exception as e:
                print(f"Error loading container_monitoring.json: {e}")

        # Load auto-update (Watchtower-style) config.
        # Precedence: built-in defaults < environment variables < UI-saved JSON.
        # This lets the updater be configured purely from docker-compose (no UI),
        # while UI changes (persisted to JSON) still take precedence when present.
        from app.models import AutoUpdateSettings

        def _env_bool(key: str, default: bool) -> bool:
            val = os.getenv(key)
            if val is None:
                return default
            return val.strip().lower() in ("1", "true", "yes", "on")

        def _env_int(key: str, default: int) -> int:
            val = os.getenv(key)
            if val is None or not val.strip():
                return default
            try:
                return int(val)
            except ValueError:
                return default

        defaults = AutoUpdateSettings()
        env_scope = (os.getenv("AUTOUPDATE_SCOPE") or defaults.scope).strip().lower()
        if env_scope not in ("opt-in", "all"):
            env_scope = defaults.scope
        self.autoupdate = AutoUpdateSettings(
            enabled=_env_bool("AUTOUPDATE_ENABLED", defaults.enabled),
            interval_seconds=_env_int("AUTOUPDATE_INTERVAL", defaults.interval_seconds),
            scope=env_scope,
            monitor_only=_env_bool("AUTOUPDATE_MONITOR_ONLY", defaults.monitor_only),
            cleanup=_env_bool("AUTOUPDATE_CLEANUP", defaults.cleanup),
            notify=_env_bool("AUTOUPDATE_NOTIFY", defaults.notify),
            respect_labels=_env_bool("AUTOUPDATE_RESPECT_LABELS", defaults.respect_labels),
        )

        # UI-saved JSON overrides env-derived defaults when it exists
        if AUTOUPDATE_CONFIG_FILE.exists():
            try:
                data = json.loads(AUTOUPDATE_CONFIG_FILE.read_text())
                self.autoupdate = AutoUpdateSettings(**data)
            except Exception as e:
                print(f"Error loading autoupdate_config.json: {e}")

        # Load Git-based apps (Coolify-style deploy-from-GitHub)
        self.git_apps = {}
        if GIT_APPS_FILE.exists():
            try:
                self.git_apps = json.loads(GIT_APPS_FILE.read_text())
            except Exception as e:
                print(f"Error loading git_apps.json: {e}")

settings = Settings()
