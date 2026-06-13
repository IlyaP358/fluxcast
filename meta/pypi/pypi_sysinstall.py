"""
fluxcast-install-system — INSTALLS D-Bus policy, .desktop file, and app icon
to their correct system locations, then optionally installs system dependencies.

Usage:
    sudo fluxcast-install-system
"""
import os
import sys
import shutil
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))

_FILES = [
    (
        os.path.join(_HERE, "_fluxcast_data", "fluxcast.desktop"),
        "/usr/share/applications/fluxcast.desktop",
    ),
    (
        os.path.join(_HERE, "_fluxcast_data", "zz-dev.fluxcast.wpa-supplicant.conf"),
        "/usr/share/dbus-1/system.d/zz-dev.fluxcast.wpa-supplicant.conf",
    ),
    (
        os.path.join(_HERE, "assets", "flcast_logo_512x512.png"),
        "/usr/share/icons/hicolor/512x512/apps/fluxcast.png",
    ),
]

# System packages required FluxCast, keyed by distro family.
_DEPS: dict[str, list[str]] = {
    "arch": [
        "pacman", "-S", "--noconfirm", "--needed",
        "networkmanager", "wpa_supplicant",
        "gstreamer", "gst-plugins-base", "gst-plugins-good",
        "gst-plugins-bad", "gst-plugins-ugly",
        "python-gobject", "python-cairo",
        "pipewire", "wireplumber",
        "ffmpeg",
    ],
    "debian": [
        "apt-get", "install", "-y",
        "network-manager", "wpasupplicant",
        "gstreamer1.0-tools",
        "gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good",
        "gstreamer1.0-plugins-bad", "gstreamer1.0-plugins-ugly",
        "gstreamer1.0-libav",
        "python3-gi", "python3-gi-cairo", "gir1.2-gtk-3.0",
        "pipewire", "wireplumber",
        "ffmpeg",
    ],
    "fedora": [
        "dnf", "install", "-y",
        "NetworkManager", "wpa_supplicant",
        "gstreamer1", "gstreamer1-plugins-base", "gstreamer1-plugins-good",
        "gstreamer1-plugins-bad-free",
        "python3-gobject", "python3-cairo",
        "pipewire", "wireplumber",
    ],
}


def _detect_distro() -> str:
    try:
        info: dict[str, str] = {}
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    info[k] = v.strip('"')
    except OSError:
        return "unknown"

    combined = f"{info.get('ID', '')} {info.get('ID_LIKE', '')}".lower()
    if "arch" in combined:
        return "arch"
    if "fedora" in combined or "rhel" in combined or "centos" in combined:
        return "fedora"
    if "debian" in combined or "ubuntu" in combined:
        return "debian"
    return "unknown"


def _ask(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _install_deps() -> None:
    family = _detect_distro()
    print(f"  Detected distro family: {family}")

    if family not in _DEPS:
        print("  Unsupported distribution.")
        print("  Please install manually: GStreamer (base/good/bad/ugly), ffmpeg,")
        print("  PyGObject, pipewire, wireplumber, networkmanager, wpa_supplicant")
        return

    if family == "fedora":
        print("  NOTE: ffmpeg, gstreamer1-libav and gstreamer1-plugins-ugly require RPM Fusion.")
        print("  To enable RPM Fusion, run first:")
        print("    sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm")
        print("  Then re-run: sudo fluxcast-install-system")
        print("  Installing available packages now (RPM Fusion packages will be skipped)...")

    cmd = _DEPS[family]
    if not shutil.which(cmd[0]):
        print(f"  ERROR: package manager '{cmd[0]}' not found in PATH.")
        return

    print(f"  Running: {cmd[0]} install ...")
    try:
        subprocess.run(cmd, check=True)
        print("  System dependencies installed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"  ERROR: installation failed (exit code {e.returncode}).")


def _post_install() -> None:
    cmds = [
        ["gtk-update-icon-cache", "-f", "/usr/share/icons/hicolor/"],
        ["update-desktop-database", "/usr/share/applications/"],
        ["systemctl", "reload", "dbus.service"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass


def main() -> None:
    if os.geteuid() != 0:
        sys.exit(
            "Error: fluxcast-install-system must be run as root.\n"
            "Try: sudo fluxcast-install-system"
        )

    print("Installing FluxCast system integration files...")
    for src, dst in _FILES:
        if not os.path.isfile(src):
            print(f"  WARNING: source not found, skipping: {src}")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  installed {dst}")

    _post_install()

    print()
    if _ask("Install system dependencies (GStreamer, ffmpeg, NetworkManager, etc.)?"):
        _install_deps()


if __name__ == "__main__":
    main()
