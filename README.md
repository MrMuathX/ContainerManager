# ContainerManager

**ContainerManager** is a self-hosted Docker dashboard built with **FastAPI** and a vanilla JS frontend. Run it as a container on your host, mount the Docker socket, and manage **all** containers from the browser: live logs, a web terminal, image updates, backups, optional **Home Assistant** MQTT discovery, **AI-assisted** help for each container, and outbound notifications.

**Repository:** [github.com/MrMuathX/ContainerManager](https://github.com/MrMuathX/ContainerManager)

## Features

| Area | What you get |
|------|----------------|
| **Dashboard** | Sortable list, search, filters, bulk actions, Excel export |
| **Detail view** | Stats, ports, mounts, labels, env, live log stream (WebSocket) |
| **Terminal** | Interactive PTY in the container (xterm.js + WebSocket) |
| **Images** | Push local images to a registry with progress feedback |
| **Updates** | Pull latest image and recreate the container |
| **Auto-Update** | Watchtower-style scheduled image checks; auto-recreate containers on new images (opt-in, monitor-only, cleanup, label-aware) |
| **Backups** | Full per-container or multi-select ZIP backups (image + writable layer + named volumes) with import/restore |
| **App settings backup** | Export/import app settings as ZIP (`system`, notifications, monitoring, AI config) |
| **Auth** | Cookie-based login (password from env / system config) |
| **Home Assistant** | MQTT discovery as Switch entities; start/stop from HA |
| **AI assistant** | Context-aware chat (OpenAI-compatible APIs) for the selected container |
| **Notifications** | Email, Telegram, Discord, webhooks; deep links to start/stop/restart |
| **Monitoring** | Background checks with optional alerting |

Interactive API docs: `/api/docs` (Swagger).

## Quick start

```bash
git clone https://github.com/MrMuathX/ContainerManager.git
cd ContainerManager

cp .env.example .env
# Edit .env — set DASHBOARD_PASSWORD and any MQTT/APP_URL values

docker compose up -d --build
```

Open the UI (default in `docker-compose.yml` maps host **8081** → app **8080**):

**http://localhost:8081**

Log in with the password from `DASHBOARD_PASSWORD` (default in examples is often `admin` — change it before exposing the service).

## Configuration

Environment variables are loaded from `.env` (see `.env.example`). Important options:

| Variable | Description |
|----------|-------------|
| `DASHBOARD_PASSWORD` | Login password — **set a strong value** default is `admin`|
| `MQTT_ENABLED` | `true` to publish HA discovery and states |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD` | Mosquitto broker |
| `HA_DISCOVERY_PREFIX` | Usually `homeassistant` |
| `STATS_INTERVAL` | Seconds between MQTT state updates |
| `APP_URL` | Base URL used for monitoring/notifications (match how you access the app) |
| `AUTOUPDATE_ENABLED` | `true` to enable the Watchtower-style auto-updater |
| `AUTOUPDATE_INTERVAL` | Poll interval in seconds (default `86400` = 24h) |
| `AUTOUPDATE_SCOPE` | `opt-in` (only flagged containers) or `all` |
| `AUTOUPDATE_MONITOR_ONLY` | `true` to check & notify only, never apply |
| `AUTOUPDATE_CLEANUP` | `true` to remove old images after updating |
| `AUTOUPDATE_NOTIFY` | `true` to send notifications on update events |
| `AUTOUPDATE_RESPECT_LABELS` | `true` to honor `com.centurylinklabs.watchtower.*` labels |

Additional settings (MQTT, AI keys, notifications) can be adjusted from the **System** and **AI** modals in the UI; persisted JSON lives under `data/` on the host (volume `./data:/app/data`).

## Home Assistant

1. Run Mosquitto (or the HA add-on) and enable the **MQTT** integration.
2. Set `MQTT_ENABLED=true` and broker credentials in `.env`.
3. Restart: `docker compose restart`.

Containers appear as **Switch** entities; you can automate start/stop/restart from HA.

## Auto-Update (Watchtower-style)

ContainerManager can automatically keep your containers up to date, inspired by
[Watchtower](https://github.com/nicholas-fedor/watchtower). On a schedule it pulls the latest
image for each eligible container, compares image digests, and — when a newer image is
found — recreates the container in place while **preserving its configuration** (env, ports,
volumes, labels, restart policy, network, command).

**Configure it** from the **Updates** button in the header:

| Option | Description |
|--------|-------------|
| **Enable auto-updater** | Master switch for the background updater |
| **Check interval** | How often to poll (default 24h, like Watchtower) |
| **Scope** | `opt-in` (only flagged containers, recommended) or `all` running containers |
| **Monitor only** | Check and notify, but never apply updates |
| **Remove old images** | Clean up the previous image after a successful update |
| **Send notifications** | Notify (email/Telegram/Discord/webhook) on update events |
| **Honor Watchtower labels** | Respect `com.centurylinklabs.watchtower.*` labels |

**Per-container** opt-in/monitor-only toggles live in each container's **Monitoring** card,
along with a **Check for update now** button. You can also use **Watchtower-compatible labels**:

```yaml
labels:
  com.centurylinklabs.watchtower.enable: "true"          # opt this container in
  com.centurylinklabs.watchtower.enable: "false"         # never auto-update this container
  com.centurylinklabs.watchtower.monitor-only: "true"    # check & notify only
```

Only containers with an actual newer image are recreated, so enabling this is safe and
idempotent. Settings persist to `data/autoupdate_config.json`.

### Configure without the UI (docker-compose)

The updater can be configured entirely from environment variables — no UI needed. Env vars
set the **defaults**; if you later change settings in the UI, the saved JSON
(`data/autoupdate_config.json`) takes precedence. Per-container opt-in still works via labels.

```yaml
services:
  containermanager:
    image: containermanager
    environment:
      AUTOUPDATE_ENABLED: "true"
      AUTOUPDATE_INTERVAL: "86400"     # 24h
      AUTOUPDATE_SCOPE: "opt-in"       # or "all"
      AUTOUPDATE_CLEANUP: "true"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data:/app/data

  # An opted-in container, Watchtower-style:
  myapp:
    image: nginx:latest
    labels:
      com.centurylinklabs.watchtower.enable: "true"
```

**Precedence:** built-in defaults → environment variables → UI-saved JSON.

## Security notes

- The stack needs access to **`/var/run/docker.sock`** — treat this as **highly privileged**.
- Do not commit `.env`; use `.env.example` as a template.
- Prefer a reverse proxy (Caddy, nginx, Traefik) with **HTTPS** for remote access.

## Project layout

```
ContainerManager/
├── app/
│   ├── main.py              # FastAPI app, auth, static frontend
│   ├── config.py            # Settings + persisted JSON paths
│   ├── models.py
│   ├── routers/
│   │   ├── containers.py    # CRUD, logs, exec WS, backup, deploy
│   │   ├── system.py        # Host stats, MQTT/system config
│   │   ├── images.py        # Registry push
│   │   └── ai.py            # AI gateway
│   └── services/
│       ├── docker_service.py
│       ├── ha_mqtt.py
│       ├── autoupdate_service.py # Watchtower-style scheduled image updates
│       ├── monitoring_service.py
│       ├── notification_service.py
│       └── ai_gateway.py
├── frontend/                # Dashboard UI (HTML/CSS/JS)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
