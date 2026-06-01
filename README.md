# FluxCast

<img src="https://fluxcast.secweb.cloud/flcast_logo_512x512.png" width="150" display="block">

FluxCast streams a Linux desktop to a TV.

## Demo

https://github.com/user-attachments/assets/ce01804c-2f86-4a5d-8ecf-d6f2a72f55d1

## Project Status

FluxCast is currently in **early $\mathbf{\color{red}!!!BETA!!!}$ testing**. 

Current validated scope:

- `wfd` is the primary path and the only mode tested as "release-ready".
- `dlna` works as fallback, using `--transport hls`.
- `cast` is experimental and currently not working in the tested Samsung setup.

The project currently focuses on **WFD/Miracast on Linux (Hyprland/wlroots class setups)**.  
DLNA and Cast are available, but they are best treated as fallback or experimental paths.

Current limitation:

- KDE/GNOME Wayland desktop capture now uses `xdg-desktop-portal` in WFD mode.
- For portal mode, install Python dependency `dbus-next` and allow screen-share in the desktop picker dialog.

## Quick Start

Default WFD run (interactive monitor/peer selection):

```bash
python3 src/main.py
```

WFD with latency/session JSONL log:

```bash
python3 src/main.py --wfd-latency-log
```

DLNA fallback:

```bash
python3 src/main.py --protocol dlna --transport hls
```

Cast mode (optional, if your TV supports it):

```bash
python3 src/main.py --protocol cast
```

Force backend manually (if auto is not suitable on your session):

```bash
python3 src/main.py --capture-backend wf-recorder
python3 src/main.py --capture-backend x11grab
python3 src/main.py --protocol wfd --wfd-capture-backend portal
python3 src/main.py --protocol wfd --wfd-capture-backend wf-recorder
python3 src/main.py --protocol wfd --wfd-capture-backend x11grab
```

## What Works Best

### WFD (Primary)

```text
screen + audio capture -> H.264/AAC RTP -> Wi-Fi Direct + RTSP -> TV WFD receiver
```

This is the lowest-latency and most predictable path in the current codebase.

### DLNA (Fallback)

```text
desktop capture -> HTTP stream -> DLNA/UPnP AVTransport -> native TV player
```

- Prefer `--transport hls` on Samsung TVs.
- `progressive-ts` can freeze or stutter on some models.

### Cast (Optional)

- Requires a TV/device with real Google Cast support.
- Requires `pychromecast`.
- Not reliable on many Samsung TV models.


## Installation

### FluxCast itself

```bash
git clone https://github.com/IlyaP358/fluxcast.git
cd fluxcast
pip install -r requirements.txt
sudo meta/install.sh
sudo systemctl reload dbus
gtk-update-icon-cache
```

[!WARNING]
If `PIP` refuses to install the required libraries to your system, you will need to do that yourself using your distro's package manager.

DLNA/Cast features require additional packages listed in `requirements.txt`.

### System tools (just as important)

WFD mode also depends on system binaries, not only Python packages:

- `ffmpeg`
- `wf-recorder` (Wayland/wlroots capture path)
- `xdg-desktop-portal` (+ desktop backend: `xdg-desktop-portal-kde` / `xdg-desktop-portal-gnome` / `xdg-desktop-portal-wlr`)
- `nmcli`, `gdbus`, `iw`, `wpa_cli` (Wi-Fi Direct and diagnostics)
- `pactl` (audio monitor autodetect)

Use:

```bash
python3 src/main.py --doctor
```

to check your machine before running WFD.

Note: on KDE/GNOME Wayland, WFD auto backend now prefers `portal` first.


## Documentation

Detailed flags, modes, and usage examples:  
[documentation/DOCUMENTATION.md](documentation/DOCUMENTATION.md)

## Tested Environment

### Hardware:

<details>
<summary>ThinkBook 14 G4+ IAP</summary>

- CPU: Intel i5-1240P (16 threads) up to 4.40 GHz
- GPU: Intel Iris Xe Graphics
- RAM: 16 GB

</details>

<details>
<summary>Dell XPS 13 Plus</summary>

- CPU: Intel i5-1260P (16 threads) up to 4.70 GHz
- GPU: Intel Iris Xe Graphics
- RAM: 16 GB LPDDR5

</details>

### Software:

<details>
<summary>Arch Linux</summary>

- Kernels: 7.0.8-arch1-1, 6.12.91-1-lts612
- WMs: Hyprland (0.55.2)
- DEs (for testing): KDE Plasma (6.6.5) | GNOME (50.1)
- Shell: zsh (5.9), fish (4.7.1)
- Terminal: kitty (0.46.2, 0.47.1)

</details>
