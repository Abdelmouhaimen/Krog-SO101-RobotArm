# 01 — Gaming PC: Screen Sender

Copy **this entire folder** to your gaming PC. It runs independently of the head tracker.
It provides a desktop UI to select a monitor, preview the captured content, and send a JPEG frame to the AI PC once every **2 seconds** (configurable).

## Windows setup

To remember the shared token, copy `.env.example` to `.env` in this folder and set
`BRIDGE_TOKEN="your Linux receiver token"`. Restart the app after editing it.
The masked Shared token field loads it automatically and remains editable.
An existing `BRIDGE_TOKEN` environment variable takes precedence. Token values
are read literally (no variable interpolation). `.env` is excluded from Git;
keep this plaintext file private. Without a saved token, enter it in the UI.

1. Install Python 3.11+ (with `py` launcher).
2. Double-click `INSTALL_WINDOWS.bat` once (internet required to install Python packages).
3. Double-click `START_WINDOWS.bat` to open the UI.
4. Set **Robot-PC IP** to its Ethernet address (example `192.168.50.1`), port **8766**, and the same shared token entered in the receiver.
5. Select which screen to capture. Display `0` is **all monitors**; choose display `1`, `2`, etc. to share just one.
6. Click **Start sharing**. Click **Stop sharing** at any time.

For Linux: `./install_linux.sh`, then `./start_linux.sh`.

## Requirements and behavior

- Start the [LAN receiver](../lan-receiver/) on the robot PC first.
- The screen app uses MSS; protected exclusive fullscreen games may capture black. Try borderless windowed mode.
- Defaults: send interval **2.0 s**, JPEG width **1600 px**, quality **75**.
- Only the selected monitor is sent, and only while Start is active. No mic/webcam access.
- Uses an authenticated **unencrypted HTTP** link: use only on trusted private Ethernet, never expose port 8766 publicly.
- Uses the receiver's in-memory `/v1/frame` endpoint; receiver drops captures after 5 s of no new frames.
- Screenshots may contain private material. Check the preview before sharing.

This program **does not** detect Valorant events or control the robot.
