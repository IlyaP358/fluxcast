"""
fluxcast-install-system — installs D-Bus policy, .desktop file, and app icon
to their correct system locations. Must be run as root.

Usage:
    sudo fluxcast-install-system
"""
import os
import sys
import shutil

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


def main() -> None:
    if os.geteuid() != 0:
        sys.exit(
            "Error: fluxcast-install-system must be run as root.\n"
            "Try: sudo fluxcast-install-system"
        )

    for src, dst in _FILES:
        if not os.path.isfile(src):
            print(f"  WARNING: source not found, skipping: {src}")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  installed {dst}")

    print(
        "\nSystem integration installed successfully."
        "\nRun the following to refresh the icon cache:"
        "\n  gtk-update-icon-cache /usr/share/icons/hicolor/"
    )


if __name__ == "__main__":
    main()
