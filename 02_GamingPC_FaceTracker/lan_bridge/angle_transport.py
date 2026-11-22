"""Keep face detection independent of network latency, always send freshest angle."""
from __future__ import annotations

import queue
import threading
import time

from .net import BridgeClient


class LatestAngleTransport:
    def __init__(self, host: str, port: int, token: str, session: str):
        self.host, self.port, self.token, self.session = host, port, token, session
        self.pending: queue.Queue = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.last_success: float = 0.0
        self.last_error: str = "Connecting"
        self.highest_seq: int = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def offer(self, packet: dict) -> None:
        self.highest_seq = max(self.highest_seq, packet["seq"])
        observation = (time.monotonic(), packet)
        try:
            self.pending.put_nowait(observation)
        except queue.Full:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self.pending.put_nowait(observation)
            except queue.Full:
                pass

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1.5)

    def _run(self):
        client = None
        try:
            client = BridgeClient(self.host, self.port, self.token)
            while not self.stop_event.is_set():
                try:
                    captured_at, packet = self.pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                if time.monotonic() - captured_at > 0.25:
                    continue  # don't replay old face positions after a network stall
                try:
                    client.post_angle(packet)
                    self.last_success = time.monotonic()
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = str(exc)[:100]
            # Never hold the last face target when sender shuts down.
            try:
                client.post_angle({"session": self.session, "seq": self.highest_seq + 1,
                                   "found": False, "angle_deg": None,
                                   "camera_angle_deg": None, "confidence": None})
            except Exception:
                pass  # receiver freshness TTL is the fallback
        except Exception as exc:
            self.last_error = str(exc)[:100]
        finally:
            if client:
                client.close()
