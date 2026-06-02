#!/usr/bin/env bash

# Check if uninstalling as root
if [[ $EUID -ne 0 ]] && [[ -z "$DESTDIR" ]]; then
    echo -e "\e[1m\e[31mERROR\e[0m: Please run this script as root"
    exit 1
fi

# Exit if a command exits with a non-zero status
set -e

# Uninstall from $DESTDIR rather than /
DESTDIR="${DESTDIR:-}"

echo -e "\e[1m\e[34m>>>\e[0m Uninstalling fluxcast from ${DESTDIR:-/}..."

# Uninstall source files & assets
rm -r "$DESTDIR/opt/fluxcast"

# Uninstall system integration
rm "$DESTDIR/usr/share/applications/fluxcast.desktop"
rm "$DESTDIR/usr/share/icons/hicolor/512x512/apps/fluxcast.png"
rm "$DESTDIR/etc/dbus-1/system.d/dev.fluxcast.wpa-supplicant.conf"
rm "$DESTDIR/usr/bin/fluxcast"

echo -e "\e[1m\e[32m>>>\e[0m Uninstalled fluxcast successfully!"
