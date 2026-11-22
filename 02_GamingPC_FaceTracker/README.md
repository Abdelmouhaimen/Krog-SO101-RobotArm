# 02 — Gaming PC: Webcam Head Tracker

Copy **this entire folder** to your gaming PC. It is independent of the screen sender.
This desktop UI opens your webcam, tracks head center using local MediaPipe face and body landmarks, computes a horizontal bearing and sends **angle numbers only** (up to 20 Hz) to the AI PC. **Webcam images never leave the gaming PC.**

## Windows setup

To remember the shared token, copy `.env.example` to `.env` in this folder and set
`BRIDGE_TOKEN="your Linux receiver token"`. Use the same token as Screen Sender
and the Linux receiver. Restart the app after editing it. The masked Shared
token field loads it automatically and remains editable. An existing
`BRIDGE_TOKEN` environment variable takes precedence. Token values are read
literally (no variable interpolation). `.env` is excluded from Git; keep this
plaintext file private. Without a saved token, enter it in the UI.

1. Install Python 3.11+.
2. Double-click `INSTALL_WINDOWS.bat` once.
3. Download the required official MediaPipe face and pose models once: `.\.venv\Scripts\python.exe -m lan_bridge.download_mediapipe`. With the Conda environment, use `conda run -n so101-m3c python -m lan_bridge.download_mediapipe`. Downloads the public model only; webcam inference stays local.
4. Double-click `START_WINDOWS.bat`.
5. Enter the AI PC Ethernet IP (example `192.168.50.1`), port **8766**, and the same shared token as the receiver.
6. Select the camera index (default 0), set horizontal FOV (default **70° is just a guess**), and adjust sign inversion / yaw offset as needed.
7. Click Start and observe webcam preview, head-center marker and bearing.

For Linux: `./install_linux.sh`, `./.venv/bin/python -m lan_bridge.download_mediapipe`, `./start_linux.sh`.

## Interpretation

- Image center = **0° relative camera bearing**; image right is positive by default.
- Uses rectilinear camera model: `atan((head_center_x - width/2) / ((width/2) / tan(HFOV/2)))`.
- HFOV must be calibrated or read from the webcam's true **horizontal** specification. Cropping and camera mirroring affect the mapping.
- Camera yaw offset **alone cannot perfectly correct for camera-to-robot translation** (different viewing origins); do not assume the reading is a precise robot bearing from a distant webcam.
- The receiver expires old angles after 0.5 s without updates.
- Tracks one person without identity recognition. Green marker: center of face outline (forehead/chin/sides), not nose position or gaze direction. This is an approximation of head center.
- If full-frame face detection fails, searches a 2x crop near the recent target (or image center after loss). Crop coordinates map back to the full frame before calculating bearing. This can help small faces; it does not add missing camera detail or guarantee a distance range.
- Orange marker / `Head ESTIMATE (body)`: when no face is detected, estimates head position above the shoulder midpoint using the torso axis. With cropped hips, uses an upright shoulder-based estimate. This can work facing away when shoulders are visible; it is not a direct measurement of the back of the head. Leaning, occlusion and extreme profiles can cause errors or loss. Multiple-person target identity is not guaranteed.
- Body estimates require shoulder visibility and presence >=0.6. Face detection/presence/tracking thresholds remain 0.5. The receiver confidence field uses the configured face-presence threshold for face observations and supporting body-landmark scores for estimates; neither is calibrated head-position confidence.
- A supported torso may estimate the head above the image: an orange upward arrow and `ESTIMATED head ABOVE VIEW` label show this explicitly. Only its horizontal coordinate contributes to the bearing; coordinates are not clamped to the image edge. Horizontally out-of-view or implausibly distant estimates remain LOST.
- LOST is sent immediately when neither source yields a valid point; no old head position is replayed. Robot calibration remains unverified.
- Additional bearing smoothing uses alpha 0.45 and resets on loss. Defaults: FOV 70° (uncalibrated), inversion off, yaw offset 180°. Camera-right is positive; center is zero.
- No facial identity recognition. Camera-to-robot calibration remains unverified.
- Up to **20 Hz**, subject to webcam FPS and CPU.

This app **never** actuates Follow Me or the robot. It reports bearings only. Keep physical control disabled until camera-to-robot calibration and actual SO-101 API integration are separately tested.

The connection is authenticated but **unencrypted HTTP**; use only on trusted private Ethernet. No cloud or paid providers are used.
