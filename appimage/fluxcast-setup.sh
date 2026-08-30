#!/usr/bin/env bash
# One-time first-run setup: installs the DBus policy for Wi-Fi Direct.
# Called by the C launcher as a subprocess before exec-ing python3.

DBUS_CONF_NAME="zz-dev.fluxcast.wpa-supplicant.conf"
DBUS_CONF_DEST="/usr/share/dbus-1/system.d/${DBUS_CONF_NAME}"
DBUS_CONF_SRC="${APPDIR}/meta/${DBUS_CONF_NAME}"

[ -f "${DBUS_CONF_SRC}" ]  || exit 0   # config missing from AppDir, skip
# Refresh when missing or content changed (e.g. insecure default-context grant).
if [ -f "${DBUS_CONF_DEST}" ] && cmp -s "${DBUS_CONF_SRC}" "${DBUS_CONF_DEST}"; then
    exit 0
fi

echo ""
if [ -f "${DBUS_CONF_DEST}" ]; then
    echo "[FluxCast] Updating DBus policy for Wi-Fi Direct..."
else
    echo "[FluxCast] First-time setup: installing DBus policy for Wi-Fi Direct..."
fi

# FUSE mounts are not accessible by root. Copy to /tmp first so sudo can read it.
TMP_CONF="$(mktemp /tmp/fluxcast-dbus-XXXXXX.conf)"
cp "${DBUS_CONF_SRC}" "${TMP_CONF}"
chmod 644 "${TMP_CONF}"

_ok=0

# GUI elevation (tray / desktop launcher context)
if [ -n "${DISPLAY}${WAYLAND_DISPLAY}" ] && command -v pkexec >/dev/null 2>&1; then
    notify-send "FluxCast — First-time setup" \
        "FluxCast needs to install a system file to enable Wi-Fi Direct device naming on your TV.\n\nYou will be asked for your password once." \
        --icon=dialog-information --urgency=normal 2>/dev/null || true
    if pkexec /bin/sh -c "install -Dm644 '${TMP_CONF}' '${DBUS_CONF_DEST}' && systemctl reload dbus" 2>/dev/null; then
        _ok=1
    fi
fi

# Terminal fallback
if [ "${_ok}" = "0" ] && command -v sudo >/dev/null 2>&1; then
    if sudo /bin/sh -c "install -Dm644 '${TMP_CONF}' '${DBUS_CONF_DEST}' && systemctl reload dbus"; then
        _ok=1
    fi
fi

rm -f "${TMP_CONF}"

if [ "${_ok}" = "1" ]; then
    echo "[FluxCast] Setup complete — Wi-Fi Direct device name enabled."
else
    echo "[FluxCast] Warning: could not install DBus policy automatically."
    echo "[FluxCast] Run this once to enable device name on TV:"
    echo "  sudo install -Dm644 '${DBUS_CONF_SRC}' '${DBUS_CONF_DEST}'"
    echo "  sudo systemctl reload dbus"
fi
echo ""
