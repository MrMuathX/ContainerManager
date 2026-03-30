import io
import uuid
import asyncio
import json
from fastapi import APIRouter, HTTPException, File, UploadFile
from fastapi.responses import StreamingResponse
import docker
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/images", tags=["images"])
client = docker.from_env()

class PullRequest(BaseModel):
    image: str

class PushRequest(BaseModel):
    image: str          # local image name/tag, e.g. "myimage:latest"
    registry: str       # registry host, e.g. "registry.example.com"
    username: Optional[str] = None
    password: Optional[str] = None

@router.get("/search")
def search_images(q: str, limit: int = 25):
    try:
        results = client.images.search(term=q, limit=limit)
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/pull")
async def pull_image(req: PullRequest):
    async def generate():
        try:
            for line in client.api.pull(req.image, stream=True, decode=True):
                status = line.get("status", "")
                progress = line.get("progress", "")
                layer = line.get("id", "")
                yield json.dumps({"status": "progress", "layer": layer, "message": f"{status} {progress}".strip()}) + "\n"
                await asyncio.sleep(0)
            yield json.dumps({"status": "done", "message": f"Successfully pulled {req.image}."}) + "\n"
        except Exception as e:
            yield json.dumps({"status": "error", "message": f"Pull failed: {e}"}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")

@router.post("/push")
async def push_image(req: PushRequest):
    """Tag local image for the target registry and push it, streaming NDJSON progress."""
    async def generate():
        try:
            # Build the full destination tag: registry/image
            image_name = req.image.lstrip("/")
            if req.registry and not image_name.startswith(req.registry):
                full_tag = f"{req.registry.rstrip('/')}/{image_name}"
            else:
                full_tag = image_name

            # Tag the local image
            local_img = client.images.get(req.image)
            # Split full_tag into repo + tag
            if ":" in full_tag.split("/")[-1]:
                repo, tag = full_tag.rsplit(":", 1)
            else:
                repo, tag = full_tag, "latest"

            local_img.tag(repo, tag=tag)
            yield json.dumps({"status": "progress", "message": f"Tagged as {repo}:{tag}"}) + "\n"
            await asyncio.sleep(0)

            # Auth config
            auth_cfg = None
            if req.username:
                auth_cfg = {"username": req.username, "password": req.password or ""}

            # Push with streaming
            for line in client.api.push(repo, tag=tag, auth_config=auth_cfg, stream=True, decode=True):
                status = line.get("status", "")
                progress = line.get("progress", "")
                error = line.get("error", "")
                if error:
                    yield json.dumps({"status": "error", "message": error}) + "\n"
                    return
                layer = line.get("id", "")
                yield json.dumps({"status": "progress", "layer": layer, "message": f"{status} {progress}".strip()}) + "\n"
                await asyncio.sleep(0)

            yield json.dumps({"status": "done", "message": f"Successfully pushed {repo}:{tag}."}) + "\n"

        except docker.errors.ImageNotFound:
            yield json.dumps({"status": "error", "message": f"Local image '{req.image}' not found."}) + "\n"
        except Exception as e:
            yield json.dumps({"status": "error", "message": f"Push failed: {e}"}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")

@router.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    try:
        content = await file.read()
        if file.filename.endswith(".tar") or file.filename.endswith(".tar.gz"):
            client.images.load(content)
            return {"success": True, "message": f"Image archive {file.filename} loaded successfully."}
        else:
            # Assume it's a Dockerfile
            tag_name = f"custom_upload:{uuid.uuid4().hex[:8]}"
            file_obj = io.BytesIO(content)
            client.images.build(fileobj=file_obj, rm=True, tag=tag_name)
            return {"success": True, "message": f"Dockerfile built successfully. Tagged as {tag_name}."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
