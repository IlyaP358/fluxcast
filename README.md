# FluxCast

<img src="https://fluxcast.secweb.cloud/flcast_logo_512x512.png" width="150" display="block">

FluxCast streams a Linux desktop to a TV.

[![Release](https://img.shields.io/github/v/release/IlyaP358/fluxcast?style=flat-square&color=green)](https://github.com/IlyaP358/fluxcast/releases)
[![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey?style=flat-square&logo=linux)](https://www.linux.org/)
[![Issues](https://img.shields.io/github/issues/IlyaP358/fluxcast?style=flat-square&color=red)](https://github.com/IlyaP358/fluxcast/issues)
[![PRs](https://img.shields.io/github/issues-pr/IlyaP358/fluxcast?style=flat-square)](https://github.com/IlyaP358/fluxcast/pulls)

> 🌐 **Need a free subdomain for your project?** Check out [sub.fluxcast.dev](https://sub.fluxcast.dev) ([GitHub repo](https://github.com/IlyaP358/fluxcast-domains)) — a free GitOps subdomain registry for developers!

## Demo

https://github.com/user-attachments/assets/ce01804c-2f86-4a5d-8ecf-d6f2a72f55d1

## Project Status

Current validated scope:

- `wfd` is the primary path and the only mode tested as release-ready.
- `dlna` works as fallback.
- `cast` is experimental and currently not working in the tested Samsung setup.

The project currently focuses on **WFD/Miracast on Linux (Hyprland/wlroots class setups)**.  
DLNA and Cast are available, but they are best treated as fallback or experimental paths.

Current limitation:

- KDE/GNOME Wayland desktop capture uses `xdg-desktop-portal` in WFD mode.
- For portal mode, install Python dependency `dbus-next` and allow screen-share in the desktop picker dialog.

## Translations

The tray picks your language up from the system locale, no configuration needed.

| Language | Maintainer |
|---|---|
| English | [IlyaP358](https://github.com/IlyaP358) |
| Русский | [IlyaP358](https://github.com/IlyaP358) |
| Čeština | [IlyaP358](https://github.com/IlyaP358) |
| Danish | [normann](https://github.com/peternormann) |
| Deutsch | [therealresonix](https://github.com/therealresonix) |
| Français | [t0mab](https://github.com/t0mab) |
| Português (Brasil) | [Jemo121](https://github.com/Jemo121) |

Adding your language means editing one JSON file and no code at all.
See [CONTRIBUTING.md](CONTRIBUTING.md#translations).

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

WFD P2P connection troubleshooting - talk to wpa_supplicant directly instead
of going through NetworkManager, and optionally pin the group to a 2.4GHz
channel for sinks that don't support Wi-Fi Direct on 5GHz:

```bash
python3 src/main.py --protocol wfd --wfd-p2p-backend wpas
python3 src/main.py --protocol wfd --wfd-p2p-backend wpas --wfd-p2p-channel 6
```

Needs the D-Bus policy in `meta/zz-dev.fluxcast.wpa-supplicant.conf`
installed to `/usr/share/dbus-1/system.d/`. On Debian/Ubuntu, a user in the
`netdev` group can then run it without sudo; elsewhere (including Arch)
it falls back to sudo - see the file for details.

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

### AppImage

Download the latest `FluxCast-x86_64.AppImage` from the [Releases](https://github.com/IlyaP358/fluxcast/releases) page, then:

```bash
chmod +x FluxCast-x86_64.AppImage
./FluxCast-x86_64.AppImage
```

On first launch FluxCast will ask for your password once to install a system file for Wi-Fi Direct.

Depending on your desktop environment, you may need to install:
- Hyprland / Sway: `wf-recorder`, `ffmpeg`
- KDE / GNOME: `gst-plugins-ugly` (package name varies by distro)

### PyPI

```bash
pip install fluxcast
sudo fluxcast-install-system
```

`fluxcast-install-system` installs the D-Bus policy, desktop entry, and system packages (GStreamer, ffmpeg, NetworkManager, etc.). Run it once after the pip install.


### Arch Linux - AUR

```bash
yay -S fluxcast-git
# or
paru -S fluxcast-git
```

### From source

```bash
git clone https://github.com/IlyaP358/fluxcast.git
cd fluxcast
pip install -r requirements.txt
sudo meta/install.sh
sudo systemctl reload dbus
sudo gtk-update-icon-cache /usr/share/icons/hicolor
```

> [!WARNING]
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

Note: on **firewalld** systems, FluxCast opens the WFD RTSP port (`7236/tcp`) for the duration of a session and closes it on exit (no-op without firewalld; disable with `--wfd-no-firewall`). See [DOCUMENTATION.md](documentation/DOCUMENTATION.md) -> "WFD and firewalld".

If you use **ufw**, open the WFD RTSP port before connecting:

```bash
sudo ufw allow 7236/tcp
```


## Documentation

Detailed flags, modes, and usage examples:  
[documentation/DOCUMENTATION.md](documentation/DOCUMENTATION.md)

## Tested Environment

### TVs and receivers:

| Device | Protocol | Source |
|---|---|---|
| Samsung UE55TU7092U | WFD, DLNA | [IlyaP358](https://github.com/IlyaP358) |
| LG OLED55B87LC | WFD | [#72](https://github.com/IlyaP358/fluxcast/issues/72) |
| LG OLED55BX9LB | WFD | [#11](https://github.com/IlyaP358/fluxcast/issues/11) |
| LG webOS UN8000PTA | WFD | [#10](https://github.com/IlyaP358/fluxcast/issues/10) |
| LG webOS SM8100PTA | WFD | [#30](https://github.com/IlyaP358/fluxcast/issues/30) |
| LG LED-43UD81 | WFD | [#44](https://github.com/IlyaP358/fluxcast/issues/44) |
| Hisense VIDAA 32A5NQ | WFD | [#56](https://github.com/IlyaP358/fluxcast/issues/56) |
| X1BQ-8461 projector | WFD | [#84](https://github.com/IlyaP358/fluxcast/issues/84) |
| Samsung UN55DU8000GXZD | WFD | [#12](https://github.com/IlyaP358/fluxcast/issues/12) |
| Samsung Galaxy Tab S9 FE | WFD | [#48](https://github.com/IlyaP358/fluxcast/pull/48) |
| Microsoft 4K Wireless Display Adapter | WFD | [#40](https://github.com/IlyaP358/fluxcast/issues/40) |
| Vizio TV with built-in Chromecast | Cast | [#51](https://github.com/IlyaP358/fluxcast/issues/51) |

### Wi-Fi adapters:

| Adapter | P2P support | Source |
|---|---|---|
| Intel AX201 | yes | [#12](https://github.com/IlyaP358/fluxcast/issues/12), [#72](https://github.com/IlyaP358/fluxcast/issues/72) |
| Intel AX210 | yes | [#87](https://github.com/IlyaP358/fluxcast/issues/87) |
| TP-Link Archer T4U Plus (USB) | yes | [#56](https://github.com/IlyaP358/fluxcast/issues/56) |
| Realtek RTL88x2bu (USB) | no | [#56](https://github.com/IlyaP358/fluxcast/issues/56) |
| Realtek RTL8822CE | no | [#45](https://github.com/IlyaP358/fluxcast/issues/45) |

### Hardware:

<details>
<summary>ThinkBook 14 G4+ IAP</summary>

- CPU: Intel i5-1240P (16 threads) up to 4.40 GHz
- GPU: Intel Iris Xe Graphics
- RAM: 16 GB

</details>

<details>
<summary>Dell XPS 13 Plus — @alba4k</summary>

- CPU: Intel i5-1260P (16 threads) up to 4.70 GHz
- GPU: Intel Iris Xe Graphics
- RAM: 16 GB LPDDR5

</details>

<details>
<summary>ThinkPad T14 Gen 4</summary>

- CPU: Intel i7-1355U (12 threads) up to 5.00 GHz
- GPU: Intel Iris Xe Graphics
- RAM: 32 GB

</details>

<details>
<summary>HP ZBook Fury G8, i9 and i5 — #72, #87</summary>

- Wi-Fi: Intel AX210
- OS: EndeavourOS, KDE Plasma (X11)

</details>

<details>
<summary>Lenovo ThinkBook 16 G6 ABP — #40</summary>

- CPU: AMD Ryzen 5 7530U
- OS: Fedora Linux 44

</details>

<details>
<summary>Lenovo IdeaPad Slim 7 Pro 14IHUS — #53</summary>

- CPU: Intel i7-11370H (8 threads) 3.30 GHz
- OS: Arch Linux

</details>

<details>
<summary>ASUS VivoBook X515JF — #44</summary>

- OS: Arch Linux (zen kernel)

</details>

<details>
<summary>Desktop PC — #12</summary>

- Wi-Fi: Intel AX201
- OS: EndeavourOS

</details>

<details>
<summary>Steam Deck — #45</summary>

- Wi-Fi: Realtek RTL8822CE
- OS: SteamOS Holo 3.7.25

</details>


### Software:

<details>
<summary>Arch Linux</summary>

- Kernels: 7.1.3-arch2-1, 7.0.8-arch1-1, 6.12.91-1-lts612
- WMs: Hyprland (0.55.4)
- DEs (for testing): KDE Plasma (6.6.5) | GNOME (50.1)
- Shell: zsh (5.9.2), fish (4.7.1)
- Terminal: kitty (0.47.4)
- Also reported: kernels 7.0.10-arch1-1, 7.0.10-zen1-1-zen, 7.0.12-arch1-1, 7.1.6-arch1-1 on KDE Plasma ([#30](https://github.com/IlyaP358/fluxcast/issues/30), [#44](https://github.com/IlyaP358/fluxcast/issues/44), [#53](https://github.com/IlyaP358/fluxcast/issues/53), [#89](https://github.com/IlyaP358/fluxcast/issues/89))

</details>

<details>
<summary>CachyOS</summary>

- Kernels: 7.0.3-1-cachyos
- DEs (for testing): KDE Plasma (6.6.4)
- Shell: bash (5.3.9)
- Terminal: konsole (26.4.0)
- Also reported: kernels 7.0.5-2, 7.0.10-2 on KDE Plasma ([#10](https://github.com/IlyaP358/fluxcast/issues/10), [#40](https://github.com/IlyaP358/fluxcast/issues/40))

</details>

<details>
<summary>EndeavourOS — #12, #72, #87</summary>

- Kernels: 7.0.5-arch1-1, 6.18.38-3-lts
- DEs: KDE Plasma (X11)

</details>

<details>
<summary>Fedora Linux 44 — #40</summary>

- Kernels: 7.0.11-200.fc44

</details>

<details>
<summary>Manjaro Linux — #84</summary>

- Kernels: 6.18.39-1-MANJARO
- DEs: GNOME (50.3)

</details>

<details>
<summary>Linux Mint 22.3 — #56</summary>

- Kernels: 6.17.0-35-generic

</details>

<details>
<summary>Xubuntu — #51</summary>

- Kernels: 6.17.0-7-generic
- DEs: XFCE (X11)

</details>

<details>
<summary>SteamOS Holo 3.7.25 — #45</summary>

- Kernels: 6.11.11-valve27

</details>
