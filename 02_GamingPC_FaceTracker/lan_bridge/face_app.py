"""Gaming PC webcam -> camera-relative angle -> robot-PC receiver, with GUI.

20 Hz is a target maximum and depends on camera FPS and detector compute time.
Only angle, confidence, and detected/lost state are transmitted; not webcam frames.
"""
from __future__ import annotations

import queue
import os
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from dotenv import dotenv_values

from .geometry import camera_bearing_deg, robot_bearing_deg, SmoothBearing
from .angle_transport import LatestAngleTransport
from .head_detector import Detector, DEFAULT_MODEL


class FaceApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SO-101 · Head Bearing Sender")
        self.geometry("760x720")
        self.host = tk.StringVar(value="192.168.50.1")
        self.port = tk.StringVar(value="8766")
        config = dotenv_values(Path(__file__).resolve().parent.parent / ".env", interpolate=False)
        self.token = tk.StringVar(value=os.environ.get("BRIDGE_TOKEN", config.get("BRIDGE_TOKEN") or ""))
        self.camera = tk.StringVar(value="0")
        self.fov = tk.StringVar(value="70")
        self.yaw = tk.StringVar(value="180")
        self.invert = tk.BooleanVar(value=False)
        self.model = tk.StringVar(value=str(DEFAULT_MODEL))
        self.status = tk.StringVar(value="Stopped · camera is not active")
        self.events: queue.Queue = queue.Queue(maxsize=3)
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.preview = None
        self._ui()
        self.after(50, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _ui(self):
        form = ttk.Frame(self, padding=12)
        form.pack(fill="x")
        fields = [("Robot-PC IP", self.host), ("Port", self.port),
                  ("Camera index", self.camera), ("Horizontal FOV (°)", self.fov),
                  ("Robot yaw offset (°)", self.yaw)]
        for i, (label, var) in enumerate(fields):
            row, col = divmod(i, 2)
            ttk.Label(form, text=label).grid(row=row, column=col*2, sticky="w", padx=3, pady=4)
            ttk.Entry(form, textvariable=var, width=20).grid(row=row, column=col*2+1, sticky="w", padx=5)
        ttk.Label(form, text="Shared token").grid(row=3, column=0, sticky="w", padx=3, pady=4)
        ttk.Entry(form, textvariable=self.token, show="•", width=55).grid(row=3, column=1, columnspan=3, sticky="ew", padx=5)
        ttk.Checkbutton(form, text="Invert horizontal direction", variable=self.invert).grid(row=4, column=0, columnspan=4, sticky="w")
        ttk.Label(form, text="MediaPipe face model").grid(row=5, column=0, sticky="w", padx=3, pady=4)
        ttk.Entry(form, textvariable=self.model, width=50).grid(row=5, column=1, columnspan=2, sticky="ew")
        ttk.Button(form, text="Browse", command=self._browse).grid(row=5, column=3)
        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=4, sticky="w", pady=9)
        self.start = ttk.Button(buttons, text="Start tracking", command=self._start)
        self.start.pack(side="left")
        self.stop = ttk.Button(buttons, text="Stop tracking", command=self._stop, state="disabled")
        self.stop.pack(side="left", padx=10)
        ttk.Label(form, textvariable=self.status, wraplength=720).grid(row=7, column=0, columnspan=4, sticky="w", pady=6)
        self.display = ttk.Label(self, anchor="center", relief="sunken")
        self.display.pack(fill="both", expand=True, padx=12, pady=9)
        ttk.Label(self,
            text="Head marker: green = face center; orange = body estimate. Camera-right = positive.\n"
                 "Only angles cross the LAN — NO webcam video. Image center = 0°.\n"
                 "For real Follow Me, the webcam's heading must be calibrated to the robot.\n"
                 "No motor command is sent by this application.",
            padding=12, justify="left").pack(anchor="w")

    def _browse(self):
        path = filedialog.askopenfilename(filetypes=[("MediaPipe model", "*.task"), ("All files", "*")])
        if path: self.model.set(path)

    def _start(self):
        try:
            conf = dict(host=self.host.get().strip(), port=int(self.port.get()), token=self.token.get().strip(),
                        camera=int(self.camera.get()), fov=float(self.fov.get()), yaw=float(self.yaw.get()),
                        invert=self.invert.get())
            if not 10 <= conf["fov"] <= 140 or not -180 <= conf["yaw"] <= 180:
                raise ValueError("FOV must be 10–140°, yaw offset -180 to +180°")
            if not conf["token"] or conf["camera"] < 0:
                raise ValueError("Set a shared token and nonnegative camera index")
            model_path = Path(self.model.get().strip()) if self.model.get().strip() else None
            if model_path and not model_path.is_file():
                raise ValueError("MediaPipe model not found. Run: python -m lan_bridge.download_mediapipe")
            conf["model"] = str(model_path) if model_path else None
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc)); return
        self.stop_event.clear()
        self.start.configure(state="disabled"); self.stop.configure(state="normal")
        self.worker = threading.Thread(target=self._run, args=(conf,), daemon=True)
        self.worker.start()

    def _stop(self):
        self.stop_event.set(); self.stop.configure(state="disabled")
        self.status.set("Stopping tracking …")

    def _emit(self, kind, value):
        try: self.events.put_nowait((kind, value))
        except queue.Full:
            try: self.events.get_nowait()
            except queue.Empty: pass
            try: self.events.put_nowait((kind, value))
            except queue.Full: pass

    def _run(self, config):
        import cv2
        from PIL import Image
        transport = None
        cap = None
        detector = None
        self._emit("status", "Opening webcam …")
        try:
            cap = cv2.VideoCapture(config["camera"])
            if not cap.isOpened():
                raise RuntimeError("Could not open webcam; try a different camera index")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            detector = Detector(config["model"], cv2)
            smoother = SmoothBearing(alpha=0.45)
            session = uuid.uuid4().hex
            transport = LatestAngleTransport(config["host"], config["port"], config["token"], session)
            transport.start()
            seq = 0
            last_sent = 0.0
            fps_last = time.monotonic()
            fps_count = 0
            fps = 0.0
            lost = False
            while not self.stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    self._emit("status", "Webcam read failed; retrying")
                    self.stop_event.wait(0.1)
                    continue
                now = time.monotonic()
                fps_count += 1
                if now - fps_last >= 1.0:
                    fps = fps_count / (now - fps_last)
                    fps_last = now
                    fps_count = 0
                face = detector.find(frame)
                angle = None
                camera_angle = None
                confidence = None
                if face is not None:
                    head_x, head_y = face.x, face.y
                    center = head_x
                    confidence = face.confidence
                    camera_angle = camera_bearing_deg(center, frame.shape[1], config["fov"])
                    raw_angle = robot_bearing_deg(camera_angle, config["yaw"], config["invert"])
                    angle = smoother.update(raw_angle)
                    lost = False
                    color = (0, 180, 255) if face.estimated else (67, 226, 115)
                    if head_y < 0:
                        cv2.arrowedLine(frame, (int(head_x), 76), (int(head_x), 54), color, 2, tipLength=0.5)
                        target_name = "ESTIMATED head ABOVE VIEW"
                    else:
                        cv2.circle(frame, (int(head_x), int(head_y)), 7, color, -1)
                        cv2.circle(frame, (int(head_x), int(head_y)), 12, color, 2)
                        target_name = 'ESTIMATED head' if face.estimated else 'Head'
                    label = f"{target_name} {camera_angle:+.1f} deg | target {angle:+.1f} deg"
                else:
                    if not lost:
                        smoother.reset()
                        lost = True
                    label = "HEAD LOST — send LOST, not center"
                cv2.line(frame, (frame.shape[1]//2, 0), (frame.shape[1]//2, frame.shape[0]), (220, 220, 220), 1)
                cv2.putText(frame, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.57, (0, 240, 240), 2)
                cv2.putText(frame, f"{fps:.1f} FPS | {detector.mode}", (8, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 240, 240), 2)
                # No image is transmitted, only bearing and presence.
                if now - last_sent >= 0.05:
                    last_sent = now
                    seq += 1
                    body = {"session": session, "seq": seq, "found": face is not None,
                            "angle_deg": angle, "camera_angle_deg": camera_angle,
                            "confidence": confidence}
                    transport.offer(body)
                    network = "connected" if time.monotonic() - transport.last_success < 0.75 else f"offline: {transport.last_error}"
                    self._emit("status", f"{fps:.1f} FPS · LAN {network} · {label}")
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                self._emit("frame", Image.fromarray(rgb))
                # Cap at 20 processed/s; do not backlog frames when detection is slow.
                self.stop_event.wait(max(0.0, 0.05 - (time.monotonic() - now)))
        except Exception as exc:
            self._emit("status", f"Tracking stopped with error: {exc}")
        finally:
            if cap is not None: cap.release()
            if detector is not None: detector.close()
            if transport is not None:
                transport.close()
            self._emit("stopped", None)

    def _poll(self):
        from PIL import ImageTk
        for _ in range(3):
            try: kind, value = self.events.get_nowait()
            except queue.Empty: break
            if kind == "status": self.status.set(value)
            elif kind == "frame":
                value.thumbnail((715, 455))
                self.preview = ImageTk.PhotoImage(value)
                self.display.configure(image=self.preview)
            elif kind == "stopped":
                self.start.configure(state="normal"); self.stop.configure(state="disabled")
                if not self.status.get().startswith("Tracking stopped with error:"):
                    self.status.set("Stopped · no face updates are being sent")
        self.after(50, self._poll)

    def _close(self):
        self.stop_event.set()
        self.destroy()


def main():
    FaceApp().mainloop()

if __name__ == "__main__":
    main()
