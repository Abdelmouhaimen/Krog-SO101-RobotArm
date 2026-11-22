"""Persistent HTTP client, used only from sender worker threads."""
from __future__ import annotations

import httpx


class BridgeClient:
    def __init__(self, host: str, port: int, token: str):
        if not token.strip():
            raise ValueError("A shared BRIDGE_TOKEN is required")
        if not host or "/" in host or not 1 <= port <= 65535:
            raise ValueError("Invalid host or port")
        self._client = httpx.Client(
            base_url=f"http://{host}:{port}",
            headers={"X-Bridge-Token": token},
            timeout=httpx.Timeout(1.0, connect=0.5),
            trust_env=False,  # do not send local telemetry through system HTTP proxy
        )

    def post_frame(self, jpeg: bytes, session: str, seq: int) -> None:
        response = self._client.post(
            "/v1/frame",
            content=jpeg,
            headers={
                "Content-Type": "image/jpeg",
                "X-Session-Id": session,
                "X-Sequence": str(seq),
            },
        )
        response.raise_for_status()

    def post_angle(self, body: dict) -> None:
        response = self._client.post("/v1/angle", json=body)
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()
