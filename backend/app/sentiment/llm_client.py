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
    tools: Optional[List[Dict]] = None,
    return_full_message: bool = False,
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
        
        if tools:
            payload["tools"] = tools
            
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
            message = data["choices"][0]["message"]
            content = message.get("content", "")
            
            # Log token usage and cost
            try:
                usage = data.get("usage", {})
                prompt_t = usage.get("prompt_tokens", 0)
                completion_t = usage.get("completion_tokens", 0)
                if prompt_t > 0 or completion_t > 0:
                    log_api_cost(provider["name"], prompt_t, completion_t)
            except Exception as ex:
                logger.warning(f"Failed to log API cost details: {ex}")
                
            # Log successful provider and exit fallback loop
            logger.info(f"[SUCCESS] Completion received from {provider['name']}.")
            if return_full_message:
                return message
            return content

        except Exception as e:
            logger.warning(f"Failed to get response from {provider['name']}: {str(e)}")
            last_error = e
            # Continue to next provider in loop

    raise RuntimeError(f"All configured LLM providers failed. Last error: {str(last_error)}")

def log_api_cost(service_name: str, prompt_tokens: int, completion_tokens: int):
    try:
        from ..database import get_db_connection
        # Rates per token in USD
        # Deepseek V3 rates: $0.14/1M input, $0.28/1M output
        # Others fallback rates: $0.50/1M input, $1.50/1M output
        name_lower = service_name.lower()
        if "deepseek" in name_lower:
            cost_usd = (prompt_tokens * 0.14 / 1000000.0) + (completion_tokens * 0.28 / 1000000.0)
        elif "groq" in name_lower:
            cost_usd = (prompt_tokens * 0.59 / 1000000.0) + (completion_tokens * 0.79 / 1000000.0)
        else:
            cost_usd = (prompt_tokens * 0.50 / 1000000.0) + (completion_tokens * 1.50 / 1000000.0)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_api_cost_ledger (
                service_name, tokens_prompt, tokens_completion, cost_usd, item_count
            ) VALUES (%s, %s, %s, %s, 1)
        """, (service_name, prompt_tokens, completion_tokens, cost_usd))
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"[COST_LEDGER] Logged {service_name} call: prompt={prompt_tokens}, comp={completion_tokens}, cost=${cost_usd:.6f}")
    except Exception as e:
        logger.warning(f"Failed to write API cost log: {e}")
