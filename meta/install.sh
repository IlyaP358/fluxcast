#!/usr/bin/env bash

# Check if installing as root
if [[ $EUID -ne 0 ]] && [[ -z "$DESTDIR" ]]; then
    echo -e "\e[1m\e[31mERROR\e[0m: Please run this script as root"
    exit 1
fi

# Exit if a command exits with a non-zero status
set -e

# Use $DESTDIR to use an install destination other than /
DESTDIR="${DESTDIR:-}"
# Use $SRCDIR to use an origin other than .
SRCDIR="${SRCDIR:-.}"

echo -e "\e[1m\e[34m>>>\e[0m Installing fluxcast to ${DESTDIR:-/}..."

# Install source files & assets
mkdir -p "$DESTDIR/opt/fluxcast"
install -m644 "$SRCDIR"/src/*.py "$DESTDIR/opt/fluxcast/"
install -Dm644 "$SRCDIR/src/assets/flcast_logo_512x512.png" -t "$DESTDIR/opt/fluxcast/assets/"

# Install system integration
install -Dm644 "$SRCDIR/meta/fluxcast.desktop" -t "$DESTDIR/usr/share/applications/"
install -Dm644 "$SRCDIR/src/assets/flcast_logo_512x512.png" "$DESTDIR/usr/share/icons/hicolor/512x512/apps/fluxcast.png"
install -Dm644 "$SRCDIR/meta/dev.fluxcast.wpa-supplicant.conf" -t "$DESTDIR/usr/share/dbus-1/system.d/"
install -Dm755 "$SRCDIR/meta/fluxcast" "$DESTDIR/usr/bin/fluxcast"

echo -e "\e[1m\e[32m>>>\e[0m Installation completed successfully!"
