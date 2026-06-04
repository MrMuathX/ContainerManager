import asyncio
import os
import tempfile
from fastapi import APIRouter, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse

from app.services import backup_jobs, docker_service
from app.models import ContainerSummary, ContainerDetail, ActionResponse, ContainerMonitoringConfig
from pydantic import BaseModel
from typing import Dict, List, Optional

class CreateContainerRequest(BaseModel):
    image: str
    name: Optional[str] = None
    env: List[str] = []
    ports: Dict[str, str] = {} # e.g. {"80/tcp": "8080"}

router = APIRouter(prefix="/api/containers", tags=["containers"])


@router.get("", response_model=list[ContainerSummary])
def list_containers(all: bool = True):
    try:
        return docker_service.list_containers(all_containers=all)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{container_id}", response_model=ContainerDetail)
def get_container(container_id: str):
    try:
        return docker_service.get_container_detail(container_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("", response_model=ActionResponse)
def create_container(req: CreateContainerRequest):
    return docker_service.create_container(req.image, req.name, req.env, req.ports)

@router.post("/{container_id}/start", response_model=ActionResponse)
def start_container(container_id: str):
    return docker_service.start_container(container_id)


@router.post("/{container_id}/stop", response_model=ActionResponse)
def stop_container(container_id: str):
    return docker_service.stop_container(container_id)


@router.post("/{container_id}/restart", response_model=ActionResponse)
def restart_container(container_id: str):
    return docker_service.restart_container(container_id)


@router.delete("/{container_id}", response_model=ActionResponse)
def remove_container(container_id: str, force: bool = True):
    return docker_service.remove_container(container_id, force=force)

from pydantic import BaseModel
class RenameRequest(BaseModel):
    new_name: str

@router.post("/{container_id}/rename", response_model=ActionResponse)
def rename_container(container_id: str, payload: RenameRequest):
    return docker_service.rename_container(container_id, payload.new_name)

@router.get("/{container_id}/monitoring", response_model=ContainerMonitoringConfig)
def get_container_monitoring(container_id: str):
    from app.services.monitoring_service import monitoring_service
    from app.services import docker_service
    # Use name as key for consistency across recreations
    c = docker_service.get_container_detail(container_id)
    return monitoring_service.get_monitoring_config(c.name)

@router.post("/{container_id}/monitoring")
def update_container_monitoring(container_id: str, config: ContainerMonitoringConfig):
    from app.services.monitoring_service import monitoring_service
    from app.services import docker_service
    c = docker_service.get_container_detail(container_id)
    monitoring_service.save_monitoring_config(c.name, config)
    return {"status": "success", "message": f"Monitoring updated for {c.name}"}



@router.get("/{container_id}/logs")
def get_logs(container_id: str, tail: int = 200):
    try:
        logs = docker_service.get_logs(container_id, tail=tail)
        return {"logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{container_id}/pull")
async def pull_image(container_id: str):
    """Stream pull+recreate progress as NDJSON."""
    async def generate():
        async for line in docker_service.update_container(container_id):
            yield line + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/{container_id}/backup/prepare")
def prepare_container_backup(container_id: str):
    """Start a background backup job. Poll /jobs/{job_id} then download when ready."""
    try:
        c = docker_service.get_container_detail(container_id)
        job_id = backup_jobs.start_container_backup_job(container_id, c.name)
        return {"job_id": job_id, "status": "running"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/jobs/{job_id}")
def container_backup_job_status(job_id: str):
    job = backup_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backup job not found.")
    return {
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "progress": job.get("progress", 0),
        "step": job.get("step", ""),
        "logs": job.get("logs", []),
        "log_seq": job.get("log_seq", 0),
    }


@router.get("/jobs/{job_id}/download")
def download_container_backup_job(job_id: str, background_tasks: BackgroundTasks):
    job = backup_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backup job not found.")
    if job["status"] == "running":
        raise HTTPException(status_code=409, detail="Backup is still running.")
    if job["status"] == "error":
        raise HTTPException(status_code=500, detail=job.get("error") or "Backup failed.")
    path = job.get("path")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=410, detail="Backup file expired or missing.")

    background_tasks.add_task(backup_jobs.release_job, job_id)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=job.get("filename") or "container_backup.zip",
    )


@router.get("/{container_id}/backup")
async def backup_container(container_id: str):
    """Download a ZIP backup of a specific container (streams from disk after build)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        c = docker_service.get_container_detail(container_id)
        filename = f"{c.name}_backup.zip"
        await asyncio.to_thread(docker_service.create_container_backup_file, container_id, tmp_path)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=str(e))

    def iterfile():
        try:
            with open(tmp_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return StreamingResponse(
        iterfile(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{container_id}/export-image")
def export_image(container_id: str):
    """Download a TAR archive of the container's image."""
    try:
        from app.services import docker_service
        gen = docker_service.export_container_image(container_id)
        c = docker_service.get_container_detail(container_id)
        image_name = c.image.replace("/", "_").replace(":", "_") if ":" in c.image else c.image
        filename = f"{image_name}.tar"
        
        return StreamingResponse(
            gen,
            media_type="application/x-tar",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/all/backup")
async def backup_all_containers():
    """Download a master ZIP containing full backups for ALL containers."""
    import zipfile
    from datetime import datetime

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    master_path = tmp.name
    tmp.close()
    try:
        all_c = docker_service.list_containers(all_containers=True)
        with zipfile.ZipFile(master_path, "w", zipfile.ZIP_DEFLATED) as z:
            for c_sum in all_c:
                c_id = c_sum.id
                c_name = c_sum.name
                fd, child_path = tempfile.mkstemp(suffix=".zip", prefix="cm-backup-")
                os.close(fd)
                try:
                    await asyncio.to_thread(docker_service.create_container_backup_file, c_id, child_path)
                    z.write(child_path, f"{c_name}/{c_name}_backup.zip")
                finally:
                    try:
                        os.unlink(child_path)
                    except OSError:
                        pass

            z.writestr(
                "manifest.txt",
                f"Master backup generated at {datetime.now().isoformat()}\nContainers: {len(all_c)}",
            )
    except Exception as e:
        try:
            os.unlink(master_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=str(e))

    def iterfile():
        try:
            with open(master_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            try:
                os.unlink(master_path)
            except OSError:
                pass

    return StreamingResponse(
        iterfile(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=all_containers_backup.zip"},
    )


@router.post("/import")
async def import_backup(
    file: UploadFile = File(...),
    container_name: Optional[str] = Form(None),
):
    """Import a full container backup ZIP and recreate container + data (NDJSON progress stream)."""
    filename = (file.filename or "").lower()
    if not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Backup file must be a .zip archive.")
    backup_bytes = await file.read()
    if not backup_bytes:
        raise HTTPException(status_code=400, detail="Uploaded backup file is empty.")

    def generate():
        for line in docker_service.import_container_backup_stream(backup_bytes, requested_name=container_name):
            yield line + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ── WebSocket: Live Logs ─────────────────────────────────────────────────────

@router.websocket("/{container_id}/ws/logs")
async def ws_logs(websocket: WebSocket, container_id: str):
    await websocket.accept()
    try:
        async for line in docker_service.stream_logs(container_id):
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(f"[error] {e}")
            await websocket.close()
        except Exception:
            pass


# ── WebSocket: Exec / Terminal ───────────────────────────────────────────────

@router.websocket("/{container_id}/ws/exec")
async def ws_exec(websocket: WebSocket, container_id: str):
    """
    Interactive terminal over WebSocket.
    Uses Docker exec with a PTY. Messages from client are stdin,
    output from exec is sent back as text.
    """
    import docker
    await websocket.accept()
    client = docker.from_env()
    try:
        c = client.containers.get(container_id)
        exec_id = client.api.exec_create(
            c.id,
            cmd="/bin/sh",
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
        )
        sock = client.api.exec_start(exec_id["Id"], socket=True, tty=True)
        # The socket is a raw socket; wrap in async tasks
        loop = asyncio.get_event_loop()

        async def read_output():
            """Read from docker exec socket and send to WebSocket."""
            while True:
                data = await loop.run_in_executor(None, sock._sock.recv, 4096)
                if not data:
                    break
                await websocket.send_bytes(data)

        async def write_input():
            """Read from WebSocket and write to docker exec socket."""
            while True:
                try:
                    msg = await websocket.receive_bytes()
                    await loop.run_in_executor(None, sock._sock.sendall, msg)
                except WebSocketDisconnect:
                    break

        await asyncio.gather(read_output(), write_input())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(f"\r\n[ContainerManager] Error: {e}\r\n")
            await websocket.close()
        except Exception:
            pass
