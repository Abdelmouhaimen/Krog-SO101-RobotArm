"""Gaming PC screen-snapshot UI. Run: python -m lan_bridge.screen_app"""
from __future__ import annotations

import io
import os
from pathlib import Path
import queue
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, messagebox
from dotenv import dotenv_values

from .net import BridgeClient


class ScreenApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SO-101 · Screen Sender")
        self.geometry("725x610")
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.events: queue.Queue = queue.Queue(maxsize=3)
        self.preview = None
        self.host = tk.StringVar(value="192.168.50.1")
        self.port = tk.StringVar(value="8766")
        config = dotenv_values(Path(__file__).resolve().parent.parent / ".env", interpolate=False)
        self.token = tk.StringVar(value=os.environ.get("BRIDGE_TOKEN", config.get("BRIDGE_TOKEN") or ""))
        self.monitor = tk.StringVar()
        self.interval = tk.StringVar(value="2.0")
        self.maxwidth = tk.StringVar(value="1600")
        self.quality = tk.StringVar(value="75")
        self.status = tk.StringVar(value="Stopped · no screen is being transmitted")
        self.monitor_map = {}
        self._ui()
        self._refresh_monitors()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _ui(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="Robot-PC IP").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.host, width=19).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(top, text="Port").grid(row=0, column=2)
        ttk.Entry(top, textvariable=self.port, width=7).grid(row=0, column=3, padx=5)
        ttk.Label(top, text="Shared token").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.token, show="•", width=44).grid(row=1, column=1, columnspan=3, sticky="ew", pady=7)
        ttk.Label(top, text="Monitor").grid(row=2, column=0, sticky="w")
        self.monitors = ttk.Combobox(top, textvariable=self.monitor, state="readonly", width=36)
        self.monitors.grid(row=2, column=1, columnspan=2, sticky="ew")
        ttk.Button(top, text="Refresh", command=self._refresh_monitors).grid(row=2, column=3, padx=5)
        ttk.Label(top, text="Interval (s)").grid(row=3, column=0, sticky="w", pady=8)
        ttk.Entry(top, textvariable=self.interval, width=9).grid(row=3, column=1, sticky="w")
        ttk.Label(top, text="Max JPEG width").grid(row=3, column=2, sticky="e")
        ttk.Entry(top, textvariable=self.maxwidth, width=8).grid(row=3, column=3, sticky="w")
        ttk.Label(top, text="JPEG quality").grid(row=4, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.quality, width=8).grid(row=4, column=1, sticky="w")
        buttons = ttk.Frame(top)
        buttons.grid(row=5, column=0, columnspan=4, sticky="w", pady=6)
        self.start = ttk.Button(buttons, text="Start sharing", command=self._start)
        self.start.pack(side="left")
        self.stop = ttk.Button(buttons, text="Stop sharing", command=self._stop, state="disabled")
        self.stop.pack(side="left", padx=10)
        ttk.Label(top, textvariable=self.status, wraplength=690).grid(row=6, column=0, columnspan=4, sticky="w", pady=8)
        ttk.Label(self, text="Preview · only the selected monitor is sent", padding=(12, 3)).pack(anchor="w")
        self.display = ttk.Label(self, anchor="center", relief="sunken")
        self.display.pack(fill="both", expand=True, padx=12, pady=8)
        ttk.Label(self, text="Frames stay in RAM on the receiver. No microphone, audio, or other monitors are captured.", padding=12).pack(anchor="w")

    def _refresh_monitors(self):
        try:
            from mss import MSS
            with MSS() as sct:
                self.monitor_map = {}
                for i, m in enumerate(sct.monitors):
                    name = f"{i}: {'All displays' if i == 0 else 'Display ' + str(i)} — {m['width']}×{m['height']}"
                    self.monitor_map[name] = i
                self.monitors["values"] = list(self.monitor_map)
                if self.monitor.get() not in self.monitor_map:
                    self.monitor.set(next((s for s in self.monitor_map if s.startswith("1:")), list(self.monitor_map)[0]))
        except Exception as exc:
            self.status.set(f"Could not detect displays: {exc}")

    def _start(self):
        try:
            config = dict(host=self.host.get().strip(), port=int(self.port.get()), token=self.token.get().strip(),
                          monitor=self.monitor_map[self.monitor.get()], interval=float(self.interval.get()),
                          maxwidth=int(self.maxwidth.get()), quality=int(self.quality.get()))
            if not 0.5 <= config["interval"] <= 60 or not 320 <= config["maxwidth"] <= 7680 or not 20 <= config["quality"] <= 95:
                raise ValueError("Interval 0.5–60 s; width 320–7680; quality 20–95")
            if not config["token"]:
                raise ValueError("A shared token is required")
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Invalid settings", str(exc)); return
        self.stop_event.clear()
        self.start.configure(state="disabled"); self.stop.configure(state="normal")
        self.worker = threading.Thread(target=self._run, args=(config,), daemon=True)
        self.worker.start()

    def _stop(self):
        self.stop_event.set()
        self.status.set("Stopping screen sharing …")
        self.stop.configure(state="disabled")

    def _emit(self, kind, value):
        try:
            self.events.put_nowait((kind, value))
        except queue.Full:
            try: self.events.get_nowait()
            except queue.Empty: pass
            try: self.events.put_nowait((kind, value))
            except queue.Full: pass

    def _run(self, config):
        from mss import MSS
        from PIL import Image
        # Keep one capture session and one HTTP client alive for the whole stream.
        client = None
        try:
            client = BridgeClient(config["host"], config["port"], config["token"])
            session = uuid.uuid4().hex
            seq = 0
            with MSS() as sct:
                monitor = sct.monitors[config["monitor"]]
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    shot = sct.grab(monitor)
                    image = Image.frombytes("RGB", shot.size, shot.rgb)
                    if image.width > config["maxwidth"]:
                        height = max(1, round(image.height * config["maxwidth"] / image.width))
                        image = image.resize((config["maxwidth"], height), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    image.save(buf, format="JPEG", quality=config["quality"], optimize=True)
                    payload = buf.getvalue()
                    seq += 1
                    try:
                        client.post_frame(payload, session, seq)
                        self._emit("status", f"Sharing display {config['monitor']} · frame {seq} · {len(payload)//1024} KiB · every {config['interval']:g}s")
                    except Exception as exc:
                        self._emit("status", f"Network unavailable (retrying): {str(exc)[:130]}")
                    self._emit("frame", image.copy())
                    self.stop_event.wait(max(0.0, config["interval"] - (time.monotonic() - started)))
        except Exception as exc:
            self._emit("status", f"Screen capture failed: {exc}")
        finally:
            if client: client.close()
            self._emit("stopped", None)

    def _poll(self):
        from PIL import ImageTk
        for _ in range(4):
            try: kind, value = self.events.get_nowait()
            except queue.Empty: break
            if kind == "status": self.status.set(value)
            elif kind == "frame":
                value.thumbnail((685, 355))
                self.preview = ImageTk.PhotoImage(value)
                self.display.configure(image=self.preview)
            elif kind == "stopped":
                self.start.configure(state="normal"); self.stop.configure(state="disabled")
                self.status.set("Stopped · no further frames will be sent")
        self.after(100, self._poll)

    def _close(self):
        self.stop_event.set()
        self.destroy()


def main():
    ScreenApp().mainloop()

if __name__ == "__main__":
    main()
