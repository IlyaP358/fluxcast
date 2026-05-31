#!/bin/bash
# Output: src/drivers/bin/hostapd

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$SCRIPT_DIR/bin/hostapd"
BUILD_DIR="/tmp/fluxcast_hostapd_build"
VERSION="2.10"
TARBALL="hostapd-${VERSION}.tar.gz"
URL="https://w1.fi/releases/${TARBALL}"

if [ -f "$OUT" ]; then
    echo "[build_hostapd] Already built: $OUT"
    echo "[build_hostapd] Delete it and re-run to rebuild."
    exit 0
fi

for tool in curl gcc make pkg-config; do
    if ! command -v "$tool" &>/dev/null; then
        echo "[build_hostapd] ERROR: '$tool' not found."
        echo "  Arch/CachyOS: sudo pacman -S base-devel"
        exit 1
    fi
done
for lib in libnl-3.0 libnl-genl-3.0 openssl; do
    if ! pkg-config --exists "$lib" 2>/dev/null; then
        echo "[build_hostapd] ERROR: dev library '$lib' not found."
        echo "  Arch/CachyOS: sudo pacman -S libnl openssl"
        exit 1
    fi
done

echo "[build_hostapd] Downloading hostapd $VERSION..."
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
curl -L --retry 3 -o "$TARBALL" "$URL"

echo "[build_hostapd] Extracting..."
tar xzf "$TARBALL"
cd "hostapd-${VERSION}"

BEACON="src/ap/beacon.c"
echo "[build_hostapd] Applying P2P wildcard probe patch to $BEACON..."

if ! grep -q "ssid_match(hapd" "$BEACON"; then
    echo "[build_hostapd] ERROR: ssid_match pattern not found in $BEACON."
    echo "  The hostapd source layout may have changed."
    exit 1
fi

sed -i \
    's/\tres = ssid_match(hapd, elems\.ssid, elems\.ssid_len,/\tif (elems.ssid_len == 7 \&\& os_memcmp(elems.ssid, "DIRECT-", 7) == 0) elems.ssid_len = 0;\n\tres = ssid_match(hapd, elems.ssid, elems.ssid_len,/' \
    "$BEACON"

if ! grep -q '"DIRECT-"' "$BEACON"; then
    echo "[build_hostapd] ERROR: patch did not apply."
    echo "  Expected 'res = ssid_match(hapd, elems.ssid, elems.ssid_len,' in $BEACON"
    grep -n "ssid_match" "$BEACON" | head -5
    exit 1
fi
echo "[build_hostapd] Patch applied:"
grep -n "DIRECT-" "$BEACON" | head -3

# Build
cd hostapd
cp defconfig .config
cat >> .config << 'EOFCFG'
CONFIG_DRIVER_NL80211=y
CONFIG_WPS=y
CONFIG_WPS2=y
CONFIG_IEEE80211N=y
EOFCFG

echo "[build_hostapd] Compiling..."
make -j"$(nproc)" 2>&1 | tail -8

mkdir -p "$SCRIPT_DIR/bin"
cp hostapd "$OUT"
echo ""
echo "[build_hostapd] Done: $OUT"
echo "[build_hostapd] Now run: sudo python3 src/main.py --protocol wfd-softap --wfd-test-pattern"
