#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== [FluxCast AppImage Build] ==="
echo "Project root : ${PROJECT_ROOT}"
echo "AppImage dir : ${SCRIPT_DIR}"

if ! command -v docker &>/dev/null; then
    echo "ERROR: docker not found. Install it first."
    exit 1
fi

docker run --rm \
  -v "${PROJECT_ROOT}:/project" \
  --privileged \
  -e APPIMAGE_EXTRACT_AND_RUN=1 \
  ubuntu:22.04 bash -c "
    set -e

    apt-get update -qq
    apt-get install -y python3-pip patchelf squashfs-tools wget file zsync gcc \
      libgdk-pixbuf2.0-bin libglib2.0-bin libgtk-3-bin gstreamer1.0-tools

    pip3 install -q appimage-builder 'packaging<22'

    wget -q -O /usr/local/bin/appimagetool \
      https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x /usr/local/bin/appimagetool

    cd /project/appimage
    rm -rf AppDir
    mkdir -p AppDir/usr/src/fluxcast
    mkdir -p AppDir/usr/bin
    mkdir -p AppDir/usr/share/icons/hicolor/512x512/apps
    mkdir -p AppDir/usr/share/pixmaps
    mkdir -p AppDir/meta

    cp -r /project/src                                              AppDir/usr/src/fluxcast/
    cp    /project/requirements.txt                                 AppDir/usr/src/fluxcast/
    cp    /project/meta/fluxcast.desktop                            AppDir/fluxcast.desktop
    cp    /project/src/assets/flcast_logo_512x512.png               AppDir/fluxcast.png
    cp    /project/src/assets/flcast_logo_512x512.png               AppDir/usr/share/icons/hicolor/512x512/apps/fluxcast.png
    cp    /project/src/assets/flcast_logo_512x512.png               AppDir/usr/share/pixmaps/fluxcast.png
    cp    /project/meta/zz-dev.fluxcast.wpa-supplicant.conf         AppDir/meta/
    cp    /project/appimage/fluxcast-setup.sh                       AppDir/usr/bin/fluxcast-setup.sh

    gcc -o AppDir/usr/bin/fluxcast-launcher /project/appimage/launcher.c
    chmod +x AppDir/usr/bin/fluxcast-launcher

    appimage-builder --recipe AppImageBuilder.yml
  "

echo "========================================="
echo "Build complete! AppImage is in: ${SCRIPT_DIR}"
echo "========================================="
