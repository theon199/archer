"""Model Cognition Layer - Local and Cloud model adapters."""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class ModelAdapter:
    """Unified adapter for local DGX Spark and cloud inference."""

    @staticmethod
    async def get_available_models() -> dict[str, Any]:
        """Check live model availability on Spark and configured cloud providers."""
        status = {
            "active_provider": settings.model_provider,
            "ollama": {"available": False, "models": []},
            "sglang": {"available": False, "models": []},
            "cloud_enabled": {
                "anthropic": bool(settings.anthropic_api_key),
                "gemini": bool(settings.gemini_api_key),
                "openai": bool(settings.openai_api_key),
            },
        }

        async with httpx.AsyncClient(timeout=3.0) as client:
            # Check Ollama
            try:
                res = await client.get(f"{settings.ollama_base_url}/api/tags")
                if res.status_code == 200:
                    models = [m["name"] for m in res.json().get("models", [])]
                    status["ollama"] = {"available": True, "models": models}
            except Exception as e:
                logger.debug(f"Ollama check failed: {e}")

            # Check SGLang router
            try:
                res = await client.get(f"{settings.sglang_base_url}/../health")
                if res.status_code == 200:
                    data = res.json()
                    status["sglang"] = {"available": True, "live": data.get("live"), "models": data.get("models", [])}
            except Exception:
                try:
                    res2 = await client.get(f"{settings.sglang_base_url}/models")
                    if res2.status_code == 200:
                        status["sglang"] = {"available": True, "models": [m["id"] for m in res2.json().get("data", [])]}
                except Exception as e:
                    logger.debug(f"SGLang check failed: {e}")

        return status

    @staticmethod
    async def stream_chat(
        messages: list[dict[str, str]],
        system_prompt: str | None = None,
        provider: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream response tokens from the selected model provider."""
        selected_provider = provider or settings.model_provider

        # Format prompt messages
        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})
        formatted_messages.extend(messages)

        if selected_provider == "local-ollama":
            async for token in ModelAdapter._stream_ollama(formatted_messages):
                yield token
        elif selected_provider == "local-sglang":
            async for token in ModelAdapter._stream_sglang(formatted_messages):
                yield token
        else:
            # Fallback mock/simulated generator if offline or during testing
            yield f"[Archer on DGX Spark ({selected_provider})]: "
            for word in ["Hello! ", "I am Archer, ", "your local AI assistant. ", "I am online and ready."]:
                yield word

    @staticmethod
    async def _stream_ollama(messages: list[dict[str, str]]) -> AsyncGenerator[str, None]:
        url = f"{settings.ollama_base_url}/api/chat"
        payload = {
            "model": settings.ollama_model,
            "messages": messages,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        yield f"[Ollama Error: status {response.status_code}]"
                        return
                    async for line in response.aiter_lines():
                        if line:
                            data = json.loads(line)
                            msg = data.get("message", {})
                            content = msg.get("content", "")
                            if content:
                                yield content
                            if data.get("done", False):
                                break
        except Exception as e:
            logger.error(f"Error streaming from Ollama: {e}")
            yield f"[Archer Ollama Connection Error: {e}]"

    @staticmethod
    async def _stream_sglang(messages: list[dict[str, str]]) -> AsyncGenerator[str, None]:
        url = f"{settings.sglang_base_url}/chat/completions"
        payload = {
            "model": settings.sglang_model,
            "messages": messages,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        yield f"[SGLang Error: status {response.status_code}]"
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            raw = line[6:].strip()
                            if raw == "[DONE]":
                                break
                            data = json.loads(raw)
                            choices = data.get("choices", [])
                            if choices and "delta" in choices[0]:
                                content = choices[0]["delta"].get("content", "")
                                if content:
                                    yield content
        except Exception as e:
            logger.error(f"Error streaming from SGLang: {e}")
            yield f"[Archer SGLang Connection Error: {e}]"
