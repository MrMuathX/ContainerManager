import httpx
import json
import os
from pydantic import BaseModel
from typing import Optional

CONFIG_FILE = "data/ai_config.json"

class AIConfig(BaseModel):
    provider: str = "openai"
    api_key: str = ""
    model: str = "gpt-4o"
    temperature: float = 0.7
    system_prompt: str = "You are a helpful Docker container management assistant."
    local_endpoint: str = "http://localhost:1234/v1"
    default_agent_name: str = "Antigravity Assistant"

def load_config() -> AIConfig:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            try:
                data = json.load(f)
                return AIConfig(**data)
            except Exception:
                pass
    return AIConfig()

def save_config(config: AIConfig):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        f.write(config.json())

async def ask_ai(prompt: str, context: Optional[str] = None) -> str:
    config = load_config()
    
    full_sys_prompt = config.system_prompt
    if context:
        full_sys_prompt += f"\n\nContext metadata:\n{context}"
        
    messages = [
        {"role": "system", "content": full_sys_prompt},
        {"role": "user", "content": prompt}
    ]
    
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    if config.provider == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        headers["Authorization"] = f"Bearer {config.api_key}"
    elif config.provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers["x-api-key"] = config.api_key
        headers["anthropic-version"] = "2023-06-01"
        payload["max_tokens"] = 1000
        system_msg = messages.pop(0)["content"]
        payload["system"] = system_msg
    elif config.provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        headers["Authorization"] = f"Bearer {config.api_key}"
    elif config.provider == "openrouter":
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers["Authorization"] = f"Bearer {config.api_key}"
    elif config.provider in ["lmstudio", "ollama", "local"]:
        url = f"{config.local_endpoint.rstrip('/')}/chat/completions"
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
    else:
        raise ValueError("Unsupported AI provider")
        
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        
        if config.provider == "anthropic":
            return data["content"][0]["text"]
        else:
            return data["choices"][0]["message"]["content"]
