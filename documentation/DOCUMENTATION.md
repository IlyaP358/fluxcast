# FluxCast Documentation

Complete reference for modes, flags, and practical command combinations.

## Quick Start

```bash
python3 src/main.py
```

By default, FluxCast starts in `wfd` mode (Miracast/Wi-Fi Display).

If you prefer not to use a terminal, launch with `--tray` to get a system tray icon — no terminal window needed:

```bash
python3 src/main.py --tray
```

## Modes

- `wfd`: **Primary recommended path** - low-latency via Wi-Fi Direct + RTSP/RTP. Works excellently on Samsung TVs.
- `dlna`: **Fallback path** - via HTTP + DLNA/UPnP TV player. Use `--transport hls` for better stability on Samsung TVs.
- `cast`: **Experimental** - Chromecast via `pychromecast`. Not supported on many Samsung TV models.

Mode selection:

```bash
python3 src/main.py --protocol wfd
python3 src/main.py --protocol dlna --transport hls
python3 src/main.py --protocol cast
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
- `--monitor NAME` pre-select monitor by name for any protocol (wfd/dlna/cast)
- `--tray` launch system tray interface (no terminal needed)

### WFD Flags

- `--wfd-scan`
- `--wfd-peer PEER`
- `--wfd-dry-run`
- `--wfd-test-pattern`
- `--wfd-ffmpeg-stats`
- `--wfd-media-pipeline auto|ffmpeg|gst`
- `--wfd-capture-backend auto|portal|wf-recorder|x11grab|gst-x11`
  - `auto` uses `portal` first on KDE/GNOME Wayland, then `wf-recorder` fallback.
- `--wfd-latency-log [PATH]`
- `--wfd-no-audio`
- `--wfd-audio-device DEVICE`
- `--wfd-rtsp-port PORT`
- `--wfd-rtp-source-port PORT`
- `--wfd-no-firewall`
- `--wfd-interface IFACE`
- `--wfd-timeout SEC`
- `--wfd-go-intent 0-15`
- `--wfd-uibc` enable the input back channel (control the desktop from the sink)
- `--wfd-monitor NAME` **deprecated** alias for `--monitor` (kept for compatibility)

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

#### Per-mode tray configuration

Tray launches can override stream defaults with an INI file at
`$XDG_CONFIG_HOME/fluxcast/config`, or `~/.config/fluxcast/config` when
`XDG_CONFIG_HOME` is not set. This file affects only the tray; direct CLI
commands keep their normal behavior.

```ini
[wfd]
output-res = 1920x1080
fps = 60
bitrate = 8M
monitor = HDMI-A-1
wfd-no-audio = false

[dlna]
transport = hls
fps = 30
bitrate = 4M

[cast]
bitrate = 4M
```

All modes accept `output-res`, `fps`, `bitrate`, and `monitor`. The `dlna` and `cast`
sections also accept `host`, `port`, `discover-timeout`, `transport`, and
`capture-backend`. The `wfd` section accepts these WFD stream/session options:

- `wfd-test-pattern`
- `wfd-media-pipeline`
- `wfd-capture-backend`
- `wfd-latency-log`
- `wfd-no-audio`
- `wfd-audio-device`
- `wfd-rtsp-port`
- `wfd-no-firewall`
- `wfd-rtp-source-port`
- `wfd-interface`
- `wfd-timeout`

`monitor` preselects the capture output for that mode by its name (as shown by
`wlr-randr`/`xrandr`, e.g. `HDMI-A-1`), so tray launches skip the monitor
picker. If omitted, the tray's own monitor selection is used.

Boolean flags accept `true` or `false` (also `yes`/`no`, `on`/`off`, and
`1`/`0`). A missing file, an empty file, or an omitted key keeps the built-in
default. Invalid values and unknown keys are logged and ignored. Device, peer,
and protocol selection remain controlled by the tray and cannot be set here.

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
  - Skip the interactive monitor picker and capture the named monitor for any protocol (wfd/dlna/cast). Use the output name as shown by `xrandr` or `wlr-randr` (e.g. `eDP-1`, `HDMI-A-1`).
  - Has no effect when WFD capture uses the xdg-desktop-portal (KDE/GNOME Wayland), where the portal dialog handles monitor selection itself.
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
  - Prefer selecting by MAC rather than index: the index can change between a
    separate `--wfd-scan` and the later connect step.
  - If omitted, FluxCast prints peers and asks for interactive selection.
- `--wfd-dry-run`
  - Prints the D-Bus connection call without activating a session.
- `--wfd-interface`
  - Explicit interface for scan path.
- `--wfd-timeout`
  - Active peer discovery timeout.
- `--wfd-go-intent`
  - Sets FluxCast's Wi-Fi Direct group-owner intent (`0`–`15`, default `0`).
  - A low value (`0` by default) is what gets most Miracast TVs to start the
    session; raise it only if a specific sink requires a higher intent.
  - Does not claim or require that the TV becomes the group owner or that the
    P2P address range changes.
- `--wfd-monitor NAME`
  - **Deprecated** alias for `--monitor`, kept for backward compatibility. Use `--monitor` instead.

### WFD Media

- `--wfd-test-pattern`
  - Uses a generated test video/audio stream instead of desktop capture.
- `--wfd-ffmpeg-stats`
  - Shows ffmpeg's live progress line (`fps`, `dup`, `drop`) for the ffmpeg senders.
  - Off by default, because the line refreshes continuously and overwrites FluxCast's own output.
  - Useful when reporting stutter, since it tells a capture problem (frames dropped or duplicated) from a link problem.
- `--wfd-media-pipeline`
  - `auto`: `gst` for test-pattern, `ffmpeg` for desktop.
  - `ffmpeg`: force ffmpeg sender.
  - `gst`: force GStreamer sender (currently mainly for test-pattern).
- `--wfd-capture-backend`
  - `auto`: tries desktop capture backends in order and falls back on startup failure.
  - `portal`: Wayland ScreenCast through xdg-desktop-portal (KDE/GNOME preferred path).
  - `wf-recorder`: recommended on Hyprland/wlroots.
  - `x11grab`: useful for X11 sessions.
  - `gst-x11`: X11 capture routed through the GStreamer MPEG-TS pipeline (the same one the test pattern uses) instead of ffmpeg. Opt-in and never chosen by `auto`. Use it when a sink connects and streams but shows a black screen on the default ffmpeg path (confirmed on Hisense Vidaa). Requires `gst-launch-1.0`, `ximagesrc` (gst-plugins-good), and `x264enc` (gst-plugins-ugly).
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
- `--wfd-no-firewall`
  - Disables the automatic firewall handling described below.

### WFD Input Back Channel (UIBC)

- `--wfd-uibc`
  - Opt-in. Lets the sink (TV/tablet) control the desktop back over the WFD
    session: touch and mouse move the cursor, and basic keyboard input is typed.
  - **Works:** touch/mouse (tap, drag), and base-character typing including
    Enter, Backspace and Tab.
  - **Limitations:**
    - Keys are injected on a **US layout**, so on other layouts some keys map
      differently (e.g. `z`/`y` on QWERTZ).
    - **No modifiers**: uppercase and combos (Shift/Ctrl/Alt) are not supported
      because the generic UIBC channel does not report which modifier is held.
    - When casting an offset/secondary monitor, touch goes to whichever output
      the cursor is currently on.

### WFD and firewalld

On **firewalld** systems the temporary Wi-Fi Direct interface (`p2p-wlan0-X`)
lands in the default zone where the RTSP port (`7236/tcp`) is closed, so the TV
can't connect back and the session never starts. To avoid this, FluxCast opens
the port while a session is active and closes it on exit:

- Only happens when **firewalld is installed and running**, otherwise it is a
  no-op (no firewall is ever touched).
- The change is **runtime only** (no `--permanent`): it disappears on a
  firewalld reload or reboot, and is removed automatically when FluxCast exits.
- A port you already opened yourself is left untouched.
- Other firewalls (`nftables`, `iptables`, `ufw`) are not modified — open the
  RTSP port manually there, e.g. `sudo ufw allow 7236/tcp`.

Pass `--wfd-no-firewall` to skip this entirely and manage the firewall yourself.

### WFD Latency Log

- `--wfd-latency-log`
  - Without an argument, writes to `/tmp/fluxcast-wfd-latency.jsonl`.
  - With an argument, writes to the specified path.
  - Format: one JSON object per line (JSONL).

Examples:

```bash
python3 src/main.py --wfd-latency-log
python3 src/main.py --wfd-latency-log /tmp/my-latency.jsonl
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
python3 src/main.py
```

### 2) WFD + telemetry latency log

```bash
python3 src/main.py --wfd-latency-log
```

### 3) WFD test-pattern smoke

```bash
python3 src/main.py --protocol wfd --wfd-test-pattern --output-res 1280x720 --bitrate 3M
```

### 4) WFD test-pattern video-only

```bash
python3 src/main.py --protocol wfd --wfd-test-pattern --wfd-no-audio --output-res 1280x720 --bitrate 3M
```

### 5) WFD desktop stable baseline

```bash
python3 src/main.py --protocol wfd --output-res 1280x720 --fps 30 --bitrate 3M --wfd-media-pipeline ffmpeg
```

### 6) WFD peer scan only

```bash
python3 src/main.py --wfd-scan
```

### 7) DLNA fallback (recommended for Samsung TVs)

```bash
python3 src/main.py --protocol dlna --transport hls
```

### 8) Cast fallback (experimental, may not work on Samsung TVs)

```bash
python3 src/main.py --protocol cast
python3 src/main.py --protocol cast --tv-ip 192.168.1.50
```

## What "Healthy" Looks Like In Logs

- `sender_health` approximately every 5 seconds.
- `processes: ... running`.
- `tx_summary` keeps increasing over time.

If these conditions hold, RTP transmission is stable. Visual quality and smoothness then mostly depend on bitrate/fps/preset and Wi-Fi radio conditions.
