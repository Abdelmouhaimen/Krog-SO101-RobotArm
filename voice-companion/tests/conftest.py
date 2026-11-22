"""Global test safety: ordinary tests can never reach a paid provider (acceptance A29).

Unless a test is marked ``live``, every WebSocket connect and every real HTTP transport request is
checked: only loopback hosts are allowed. API keys from the developer's environment or ``.env`` are
removed, so a bug cannot silently use real credentials.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
import websockets

LOCAL = {"127.0.0.1", "localhost", "::1"}
BLOCKED: list[str] = []


def _check(url: str) -> None:
    host = urlparse(str(url)).hostname or ""
    if host not in LOCAL:
        BLOCKED.append(str(url))
        raise RuntimeError(f"network access to {host!r} is blocked in offline tests (use -m live)")


@pytest.fixture(autouse=True)
def _offline_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live"):
        return
    for k in ("DEEPGRAM_API_KEY", "OPENAI_API_KEY", "SO101_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    real_connect = websockets.connect

    def guarded_connect(uri: str, *a: Any, **kw: Any) -> Any:
        _check(uri)
        return real_connect(uri, *a, **kw)

    monkeypatch.setattr(websockets, "connect", guarded_connect)
    real_send = httpx.AsyncHTTPTransport.handle_async_request

    async def guarded_send(self: httpx.AsyncHTTPTransport, req: httpx.Request) -> httpx.Response:
        _check(str(req.url))
        return await real_send(self, req)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_send)
