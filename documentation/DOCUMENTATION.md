# FluxCast Documentation

Complete reference for modes, flags, and practical command combinations.

## Quick Start

```bash
python3 main.py
```

By default, FluxCast starts in `wfd` mode (Miracast/Wi-Fi Display).

If you prefer not to use a terminal, launch with `--tray` to get a system tray icon — no terminal window needed:

```bash
python3 main.py --tray
```

## Modes

- `wfd`: **Primary recommended path** - low-latency via Wi-Fi Direct + RTSP/RTP. Works excellently on Samsung TVs.
- `dlna`: **Legacy fallback path** - via HTTP + DLNA/UPnP TV player. Use `--transport hls` for better stability on Samsung TVs.
- `cast`: **Experimental/NOT TESTED** - Chromecast via `pychromecast`. Not supported on many Samsung TV models.

Mode selection:

```bash
python3 main.py --protocol wfd
python3 main.py --protocol dlna --transport hls
python3 main.py --protocol cast
```

## Full CLI Flags

### General Flags

- `--protocol dlna|cast|wfd`
- `--host HOST`
- `--port PORT`
- `--output-res WxH`
- `--fps N`
- `--bitrate Xm`
- `--discover-timeout N`
- `--capture-backend auto|wf-recorder|x11grab`
- `--transport progressive-ts|hls|live-ts`
- `--doctor`
- `--doctor-json`
- `--tv-ip IP` (for `cast` only)
- `--device-name NAME` pre-select DLNA/Cast device by friendly name
- `--monitor NAME` pre-select monitor by name for DLNA/Cast capture
- `--tray` launch system tray interface (no terminal needed)

### WFD Flags

- `--wfd-scan`
- `--wfd-peer PEER`
- `--wfd-dry-run`
- `--wfd-test-pattern`
- `--wfd-media-pipeline auto|ffmpeg|gst`
- `--wfd-capture-backend auto|portal|wf-recorder|x11grab`
  - `auto` uses `portal` first on KDE/GNOME Wayland, then `wf-recorder` fallback.
- `--wfd-latency-log [PATH]`
- `--wfd-no-audio`
- `--wfd-audio-device DEVICE`
- `--wfd-rtsp-port PORT`
- `--wfd-rtp-source-port PORT`
- `--wfd-interface IFACE`
- `--wfd-timeout SEC`
- `--wfd-monitor NAME` pre-select monitor by name for WFD capture (skips interactive picker)

## Flag Details

### Core Flags

- `--protocol`
  - Default: `wfd`.
  - `dlna` and `cast` are fallback/alternative paths.
- `--output-res`
  - Example: `1280x720`, `1920x1080`.
  - In `wfd`, affects negotiated media mode and scaling.
- `--fps`
  - Recommended for stability: `30`.
- `--bitrate`
  - Formats: `3000k`, `3M`, `5M`.
  - Desktop WFD has a quality floor (the code may automatically raise a too-low bitrate).

### System Tray

- `--tray`
  - Launches a system tray icon instead of a terminal session. Scan, select device and monitor, start/stop casting. All from the tray menu.
  - Requires `libappindicator` (Hyprland/KDE) or `gnome-shell-extension-appindicator` (GNOME).
  - On non-Hyprland Wayland (KDE, GNOME), WFD capture uses the xdg-desktop-portal screen picker dialog.

### DLNA/Cast

- `--host`, `--port`
  - HTTP server address and port for DLNA/Cast streams.
- `--discover-timeout`
  - Discovery timeout for DLNA/Cast.
- `--capture-backend`
  - `auto`: selects backend by session and retries fallback backend on startup failure.
  - `wf-recorder`: preferred for Hyprland/wlroots.
  - `x11grab`: useful for X11 sessions.
- `--transport`
  - `hls`: **Recommended for Samsung TVs** - more stable HLS streaming
  - `progressive-ts`: May cause freezing on some Samsung TV models
  - `live-ts`: Experimental live MPEG-TS transport
- `--tv-ip`
  - For `cast`: direct IP connection without discovery (may not work on Samsung TVs).
- `--device-name NAME`
  - Skip the interactive device picker and connect directly to the named DLNA or Chromecast device. Match is by friendly name (exact string as reported by the device).
  - Example: `--device-name "Samsung TV"`
- `--monitor NAME`
  - Skip the interactive monitor picker and capture the named monitor for DLNA/Cast streams. Use the output name as shown by `xrandr` or `wlr-randr` (e.g. `eDP-1`, `HDMI-A-1`).
  - Example: `--monitor eDP-1`

### Diagnostics

- `--doctor`
  - Human-readable capability report.
- `--doctor-json`
  - Same report in JSON for automation.

### WFD Discovery/Connect

- `--wfd-scan`
  - Scan only, no connection attempt.
- `--wfd-peer`
  - Accepts index, MAC, or device-name substring.
  - If omitted, FluxCast prints peers and asks for interactive selection.
- `--wfd-dry-run`
  - Prints the D-Bus connection call without activating a session.
- `--wfd-interface`
  - Explicit interface for scan path.
- `--wfd-timeout`
  - Active peer discovery timeout.
- `--wfd-monitor NAME`
  - Skip the interactive monitor picker for WFD capture. Accepts the output name (e.g. `eDP-1`). Has no effect when WFD capture uses the xdg-desktop-portal (KDE/GNOME Wayland), where the portal dialog handles monitor selection itself.
  - Example: `--wfd-monitor eDP-1`

### WFD Media

- `--wfd-test-pattern`
  - Uses a generated test video/audio stream instead of desktop capture.
- `--wfd-media-pipeline`
  - `auto`: `gst` for test-pattern, `ffmpeg` for desktop.
  - `ffmpeg`: force ffmpeg sender.
  - `gst`: force GStreamer sender (currently mainly for test-pattern).
- `--wfd-capture-backend`
  - `auto`: tries desktop capture backends in order and falls back on startup failure.
  - `portal`: Wayland ScreenCast through xdg-desktop-portal (KDE/GNOME preferred path).
  - `wf-recorder`: recommended on Hyprland/wlroots.
  - `x11grab`: useful for X11 sessions.
  - `portal` backend requirements: `dbus-next`, `xdg-desktop-portal`, desktop portal backend, and `gst-launch-1.0`.
- `--wfd-no-audio`
  - **Video-only mode** - May cause immediate disconnects on Samsung TVs during WFD negotiation.
  - Use primarily for diagnostic/testing purposes.
- `--wfd-audio-device`
  - Explicit Pulse/PipeWire monitor source.
- `--wfd-rtsp-port`
  - RTSP port in WFD source IE (usually does not need changes).
- `--wfd-rtp-source-port`
  - Local RTP source port.

### WFD Latency Log

- `--wfd-latency-log`
  - Without an argument, writes to `/tmp/fluxcast-wfd-latency.jsonl`.
  - With an argument, writes to the specified path.
  - Format: one JSON object per line (JSONL).

Examples:

```bash
python3 main.py --wfd-latency-log
python3 main.py --wfd-latency-log /tmp/my-latency.jsonl
```

## Latency Log Events

- `rtsp_connected`
  - Source accepted incoming TCP/RTSP connection from sink.
- `media_starting`
  - Sender process startup began.
- `play_accepted`
  - Includes `setup_ms`: time from `rtsp_connected` to accepted `PLAY`.
- `latency_probe`
  - Includes `sender_startup_ms`: from `PLAY accepted` to first transmitted RTP bytes.
  - Includes `sender_path_latency_ms`: `setup_ms + sender_startup_ms`.
  - This is an accurate sender-path latency metric inside FluxCast (excludes TV decode/render delay).
- `sender_health`
  - Periodic telemetry of process health and transmitted-byte counter.

## Practical Command Combinations

### 1) Default WFD run (interactive)

```bash
python3 main.py
```

### 2) WFD + telemetry latency log

```bash
python3 main.py --wfd-latency-log
```

### 3) WFD test-pattern smoke

```bash
python3 main.py --protocol wfd --wfd-test-pattern --output-res 1280x720 --bitrate 3M
```

### 4) WFD test-pattern video-only

```bash
python3 main.py --protocol wfd --wfd-test-pattern --wfd-no-audio --output-res 1280x720 --bitrate 3M
```

### 5) WFD desktop stable baseline

```bash
python3 main.py --protocol wfd --output-res 1280x720 --fps 30 --bitrate 3M --wfd-media-pipeline ffmpeg
```

### 6) WFD peer scan only

```bash
python3 main.py --wfd-scan
```

### 7) DLNA fallback (recommended for Samsung TVs)

```bash
python3 main.py --protocol dlna --transport hls
```

### 8) Cast fallback (experimental, may not work on Samsung TVs)

```bash
python3 main.py --protocol cast
python3 main.py --protocol cast --tv-ip 192.168.1.50
```

## What "Healthy" Looks Like In Logs

- `sender_health` approximately every 5 seconds.
- `processes: ... running`.
- `tx_summary` keeps increasing over time.

If these conditions hold, RTP transmission is stable. Visual quality and smoothness then mostly depend on bitrate/fps/preset and Wi-Fi radio conditions.
