# backend/app/sentiment/llm_client.py
import requests
import logging
from typing import Dict, List, Optional
from ..config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

def send_chat_completion(
    messages: List[Dict[str, str]],
    temperature: float = 0.2,
    response_format: Optional[Dict] = None,
    timeout: int = 60
) -> str:
    """
    Unified LLM router that sends chat completion requests to DeepSeek,
    with cascading fallbacks to Groq, NVIDIA NIM, and OpenRouter if
    servers are down or keys are not provided.
    """
    providers = []

    # 1. DeepSeek (Primary)
    if settings.deepseek_api_key:
        providers.append({
            "name": "DeepSeek",
            "api_key": settings.deepseek_api_key,
            "base_url": settings.deepseek_base_url,
            "model": settings.deepseek_model,
            "headers": {
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json"
            }
        })

    # 2. Groq (Fallback 1)
    if settings.groq_api_key:
        providers.append({
            "name": "Groq",
            "api_key": settings.groq_api_key,
            "base_url": settings.groq_base_url,
            "model": settings.groq_model,
            "headers": {
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json"
            }
        })

    # 3. NVIDIA NIM (Fallback 2)
    if settings.nvidia_api_key:
        providers.append({
            "name": "NVIDIA NIM",
            "api_key": settings.nvidia_api_key,
            "base_url": settings.nvidia_base_url,
            "model": settings.nvidia_model,
            "headers": {
                "Authorization": f"Bearer {settings.nvidia_api_key}",
                "Content-Type": "application/json"
            }
        })

    # 4. OpenRouter (Fallback 3)
    if settings.openrouter_api_key:
        providers.append({
            "name": "OpenRouter",
            "api_key": settings.openrouter_api_key,
            "base_url": settings.openrouter_base_url,
            "model": settings.openrouter_model,
            "headers": {
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/Roadlion/MIMIR-new",
                "X-Title": "MIMIR"
            }
        })

    if not providers:
        raise ValueError("No LLM provider keys (DeepSeek, Groq, NVIDIA, OpenRouter) configured in environment.")

    last_error = None
    for provider in providers:
        logger.info(f"Attempting chat completion via {provider['name']} using model {provider['model']}...")
        
        payload = {
            "model": provider["model"],
            "messages": messages,
            "temperature": temperature
        }
        
        # Include response_format if specified (and if the provider supports it)
        # Note: Groq, DeepSeek, OpenRouter generally support JSON Mode via response_format.
        if response_format:
            payload["response_format"] = response_format

        try:
            url = f"{provider['base_url']}/chat/completions"
            resp = requests.post(
                url,
                headers=provider["headers"],
                json=payload,
                timeout=timeout,
                verify=False
            )
            
            # If rate limit or other server issues, fall back
            if resp.status_code != 200:
                logger.warning(f"{provider['name']} returned error status {resp.status_code}: {resp.text}")
                resp.raise_for_status()
                
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            
            # Log successful provider and exit fallback loop
            logger.info(f"[SUCCESS] Completion received from {provider['name']}.")
            return content

        except Exception as e:
            logger.warning(f"Failed to get response from {provider['name']}: {str(e)}")
            last_error = e
            # Continue to next provider in loop

    raise RuntimeError(f"All configured LLM providers failed. Last error: {str(last_error)}")
