"""Runs the FastAPI app with uvicorn in a background thread of the application process (ADR-018).

The HTTP server is the second interface of the ONE controller: it shares the coordinator (through its
request queue and published snapshot), the Behavior/Pose libraries and the command ledger with the
Studio window. It never owns the serial port.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from so101_companion.config import LOOPBACK_HOSTS, ApiSection


class ApiServer:
    def __init__(self, app: Any, api: ApiSection) -> None:
        import uvicorn

        if api.host not in LOOPBACK_HOSTS and not api.allow_remote:
            raise RuntimeError(f"refusing to bind {api.host}: remote API access is disabled")
        config = uvicorn.Config(
            app,
            host=api.host,
            port=api.port,
            log_level="warning",
            access_log=False,
            ws="websockets-sansio",  # ONLY for the authenticated Follow Me target stream (ADR-019, D7)
            lifespan="off",
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        # uvicorn only captures signals on the main thread; this thread leaves them to the app
        self.thread = threading.Thread(target=self.server.run, name="so101-http-api", daemon=True)
        self.api = api
        self.video: Any = None  # the SimulationVideo served by this app (closed on stop)

    @property
    def url(self) -> str:
        host = f"[{self.api.host}]" if ":" in self.api.host else self.api.host
        return f"http://{host}:{self.bound_port}"

    @property
    def bound_port(self) -> int:
        servers = getattr(self.server, "servers", None) or []
        for srv in servers:
            for sock in getattr(srv, "sockets", []) or []:
                return int(sock.getsockname()[1])
        return self.api.port

    def start(self, timeout: float = 10.0) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError(f"HTTP API failed to start on {self.api.host}:{self.api.port}")
            if time.monotonic() > deadline:
                raise RuntimeError("HTTP API did not start in time")
            time.sleep(0.02)

    def stop(self, timeout: float = 5.0) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout)
        if self.video is not None:
            self.video.close()
