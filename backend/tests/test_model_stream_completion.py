"""Explicit HTTP protocol fixtures, not evidence of a real inference integration."""

import httpx
import pytest

from app.cognition.models import ModelAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,body,expected",
    [
        ("ollama", b'{"message":{"content":"partial"},"done":false}\n', False),
        ("ollama", b'{"message":{"content":"complete"},"done":true}\n', True),
        ("ollama", b'{"error":"synthetic secret error","done":true}\n', False),
        ("ollama", b'{"message":{"content":"partial"},"done":"false"}\n', False),
        ("sglang", b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', False),
        ("sglang", b'data: {"choices":[{"delta":{"content":"complete"}}]}\n\ndata: [DONE]\n\n', True),
    ],
)
async def test_completion_markers_required(monkeypatch, provider, body, expected):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(
        "app.cognition.models.httpx.AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    stream = ModelAdapter._stream_ollama([]) if provider == "ollama" else ModelAdapter._stream_sglang([])
    if expected:
        assert "".join([token async for token in stream]) == "complete"
    else:
        with pytest.raises(RuntimeError, match="stream failed") as failure:
            _ = [token async for token in stream]
        assert "synthetic secret" not in str(failure.value)
