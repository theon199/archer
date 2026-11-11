"""Model Cognition Layer - Local and Cloud model adapters."""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.config import settings
from app.readiness import model_ready, validate_runtime

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
                "anthropic": False,
                "gemini": False,
                "openai": False,
                "deepseek": False,
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
    async def chat_with_tools_step(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Execute a single structured agent step with the local model, supporting tools and reasoning."""
        selected_provider = provider or settings.model_provider
        if selected_provider not in {"local-ollama", "local-sglang"}:
            raise ValueError("Cloud inference is disabled; select a local provider")
        validate_runtime(selected_provider)
        if settings.expected_model_digest:
            if selected_provider != settings.model_provider or not await model_ready():
                raise RuntimeError("Pinned local model is unavailable or its digest changed")

        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})
        formatted_messages.extend(messages)

        if selected_provider == "local-ollama":
            return await ModelAdapter._chat_step_ollama(formatted_messages, tools)
        elif selected_provider == "local-sglang":
            return await ModelAdapter._chat_step_sglang(formatted_messages, tools)
        else:
            return {
                "role": "assistant",
                "content": f"Archer is online on NVIDIA DGX Spark ({selected_provider}).",
                "thinking": "",
                "tool_calls": [],
            }

    @staticmethod
    async def _chat_step_ollama(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        url = f"{settings.ollama_base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": settings.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_ctx": settings.ollama_num_ctx,
                "temperature": settings.ollama_temperature,
            },
        }
        if tools:
            payload["tools"] = tools

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                res = await client.post(url, json=payload)
                res.raise_for_status()
                msg = res.json()["message"]
                return {
                    "role": "assistant",
                    "content": msg.get("content", ""),
                    "thinking": msg.get("thinking", ""),
                    "tool_calls": msg.get("tool_calls", []),
                }
        except Exception:
            raise RuntimeError("Local Ollama request failed") from None

    @staticmethod
    async def _chat_step_sglang(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        url = f"{settings.sglang_base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": settings.sglang_model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                res = await client.post(url, json=payload)
                res.raise_for_status()
                msg = res.json()["choices"][0]["message"]
                return {
                    "role": "assistant",
                    "content": msg.get("content", ""),
                    "thinking": "",
                    "tool_calls": msg.get("tool_calls", []),
                }
        except Exception:
            raise RuntimeError("Local SGLang request failed") from None

    @staticmethod
    async def stream_chat(
        messages: list[dict[str, Any]],
        system_prompt: str | None = None,
        provider: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream response tokens from the selected model provider."""
        selected_provider = provider or settings.model_provider
        if selected_provider not in {"local-ollama", "local-sglang"}:
            raise ValueError("Cloud inference is disabled; select a local provider")
        validate_runtime(selected_provider)
        if settings.expected_model_digest:
            if selected_provider != settings.model_provider or not await model_ready():
                raise RuntimeError("Pinned local model is unavailable or its digest changed")

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

    @staticmethod
    async def _stream_ollama(messages: list[dict[str, Any]]) -> AsyncGenerator[str, None]:
        url = f"{settings.ollama_base_url}/api/chat"
        payload = {
            "model": settings.ollama_model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_ctx": settings.ollama_num_ctx,
                "temperature": settings.ollama_temperature,
            },
        }
        completed = False
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        raise RuntimeError("Local Ollama returned an error")
                    async for line in response.aiter_lines():
                        if line:
                            data = json.loads(line)
                            if "error" in data:
                                raise RuntimeError("Local Ollama stream returned an error")
                            msg = data.get("message", {})
                            content = msg.get("content", "")
                            if content:
                                yield content
                            if data.get("done") is True:
                                completed = True
                                break
                    if not completed:
                        raise RuntimeError("Local Ollama stream ended before completion")
        except Exception:
            raise RuntimeError("Local Ollama stream failed") from None

    @staticmethod
    async def _stream_sglang(messages: list[dict[str, str]]) -> AsyncGenerator[str, None]:
        url = f"{settings.sglang_base_url}/chat/completions"
        payload = {
            "model": settings.sglang_model,
            "messages": messages,
            "stream": True,
        }
        completed = False
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        raise RuntimeError("Local SGLang returned an error")
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            raw = line[6:].strip()
                            if raw == "[DONE]":
                                completed = True
                                break
                            data = json.loads(raw)
                            choices = data.get("choices", [])
                            if choices and "delta" in choices[0]:
                                content = choices[0]["delta"].get("content", "")
                                if content:
                                    yield content
                    if not completed:
                        raise RuntimeError("Local SGLang stream ended before completion")
        except Exception:
            raise RuntimeError("Local SGLang stream failed") from None

    @staticmethod
    async def _stream_deepseek(messages: list[dict[str, str]]) -> AsyncGenerator[str, None]:
        if not settings.deepseek_api_key:
            yield "[DeepSeek Error: DEEPSEEK_API_KEY is not configured in .env]"
            return

        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.deepseek_model,
            "messages": messages,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        err_text = await response.aread()
                        yield f"[DeepSeek API Error: status {response.status_code} - {err_text.decode('utf-8', errors='ignore')}]"
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            raw = line[6:].strip()
                            if raw == "[DONE]":
                                break
                            data = json.loads(raw)
                            choices = data.get("choices", [])
                            if choices and "delta" in choices[0]:
                                delta = choices[0]["delta"]
                                # Support reasoning_content for DeepSeek-R1
                                reason = delta.get("reasoning_content", "")
                                content = delta.get("content", "")
                                if reason:
                                    yield reason
                                if content:
                                    yield content
        except Exception as e:
            logger.error(f"Error streaming from DeepSeek: {e}")
            yield f"[Archer DeepSeek Connection Error: {e}]"
