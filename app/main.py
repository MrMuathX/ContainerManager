import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import containers, system
from app.services import docker_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Auth Helpers ─────────────────────────────────────────────────────────────

SESSION_COOKIE = "cm_session"
_VALID_TOKEN = settings.DASHBOARD_PASSWORD  # simple shared-secret auth


def _check_auth(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return token == _VALID_TOKEN


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start MQTT background task if enabled
    task = None
    if settings.MQTT_ENABLED:
        from app.services.ha_mqtt import mqtt_background_task
        task = asyncio.create_task(
            mqtt_background_task(lambda: docker_service.list_containers())
        )
        logger.info("MQTT background task started.")
    else:
        logger.info("MQTT disabled. Set MQTT_ENABLED=true to enable HA integration.")

    # Start Monitoring background task
    from app.services.monitoring_service import monitoring_background_task
    m_task = asyncio.create_task(monitoring_background_task(settings.APP_URL))
    logger.info("Monitoring background task started.")

    # Start Auto-Update (Watchtower-style) background task
    from app.services.autoupdate_service import autoupdate_background_task
    au_task = asyncio.create_task(autoupdate_background_task())
    logger.info("Auto-update background task started.")

    yield

    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    m_task.cancel()
    try:
        await m_task
    except asyncio.CancelledError:
        pass

    au_task.cancel()
    try:
        await au_task
    except asyncio.CancelledError:
        pass


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="ContainerManager",
    description="Docker container management dashboard with Home Assistant integration",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.get("/login", include_in_schema=False)
async def login_page():
    import os
    file_path = os.path.join("frontend", "login.html")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    # Fallback to a very basic error message if file is missing
    return HTMLResponse("<h1>Login page not found</h1>", status_code=404)


@app.post("/api/auth/login", include_in_schema=False)
async def login(request: Request):
    body = await request.json()
    if body.get("password") == settings.DASHBOARD_PASSWORD:
        response = JSONResponse({"ok": True})
        response.set_cookie(SESSION_COOKIE, settings.DASHBOARD_PASSWORD, httponly=True, samesite="lax")
        return response
    raise HTTPException(status_code=401, detail="Invalid password")


@app.post("/api/auth/logout", include_in_schema=False)
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Auth middleware (protect everything except /login and /api/auth) ──────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Allow login page and auth endpoints without cookies
    if path.startswith("/api/auth") or path == "/login":
        return await call_next(request)
    # WebSocket upgrade: check cookie from query param fallback
    if not _check_auth(request):
        if path.startswith("/api") or path.startswith("/ws"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


# ── Include routers ───────────────────────────────────────────────────────────

from app.routers import images, ai

app.include_router(containers.router)
app.include_router(system.router)
app.include_router(images.router)
app.include_router(ai.router)

# ── Action Routes (for notifications) ──────────────────────────────────────────

@app.get("/action/{action}/{name}", include_in_schema=False)
async def external_action(action: str, name: str, request: Request):
    # This endpoint is targeted by notification links
    # If not authorized, it will be redirected to /login by the middleware
    # After login, the user should be redirected back here? 
    # Current middleware doesn't support 'next' param, but we can just redirect to / after action.
    
    try:
        from app.services import docker_service
        # Find container by name
        all_c = docker_service.list_containers()
        target = next((c for c in all_c if c.name == name.lstrip("/")), None)
        
        if not target:
            return HTMLResponse(f"<h1>Container {name} not found</h1><a href='/'>Back to Dashboard</a>")
        
        if action == "start":
            docker_service.start_container(target.id)
        elif action == "stop":
            docker_service.stop_container(target.id)
        elif action == "restart":
            docker_service.restart_container(target.id)
            
        return HTMLResponse(f"<h1>Action {action} successful for {name}</h1><script>setTimeout(() => window.location.href='/', 2000);</script><p>Redirecting to dashboard...</p>")
    except Exception as e:
        return HTMLResponse(f"<h1>Error</h1><p>{str(e)}</p><a href='/'>Back to Dashboard</a>")

# ── Serve frontend static files ───────────────────────────────────────────────

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
