from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services.ai_gateway import AIConfig, load_config, save_config, ask_ai
from app.services import docker_service
import traceback

router = APIRouter(prefix="/api/ai", tags=["ai"])

class AskRequest(BaseModel):
    prompt: str
    container_id: str | None = None

@router.get("/config", response_model=AIConfig)
def get_ai_config():
    return load_config()

@router.post("/config")
def update_ai_config(config: AIConfig):
    save_config(config)
    return {"success": True}

@router.post("/ask")
async def ask_question(req: AskRequest):
    context = ""
    if req.container_id:
        try:
            detail = docker_service.get_container_detail(req.container_id)
            logs = docker_service.get_logs(req.container_id, tail=50)
            context += f"Container Name: {detail.name}\n"
            context += f"Status: {detail.status}\n"
            context += f"Image: {detail.image}\n"
            context += f"Recent Logs:\n{logs}\n"
            if detail.stats:
                context += f"CPU: {detail.stats.cpu_percent}%\n"
                context += f"MEM: {detail.stats.mem_usage_mb}MB\n"
        except Exception:
            pass
            
    try:
        reply = await ask_ai(req.prompt, context=context)
        return {"reply": reply}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
