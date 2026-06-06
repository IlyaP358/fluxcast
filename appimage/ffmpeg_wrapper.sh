#!/bin/bash
# ffmpeg wrapper for FluxCast AppImage.

_bundled="${APPDIR:-}/usr/lib/fluxcast/ffmpeg-static"

if [ -n "${APPDIR:-}" ]; then
    _ld=""
    IFS=':' read -ra _parts <<< "${LD_LIBRARY_PATH:-}"
    for _p in "${_parts[@]}"; do
        [[ "$_p" == "${APPDIR}"* ]] || _ld+="${_ld:+:}${_p}"
    done
    export LD_LIBRARY_PATH="${_ld}"
    unset LD_PRELOAD
fi

_needs_pulse=0
_prev=""
for _arg in "$@"; do
    if [ "$_prev" = "-f" ] && [ "$_arg" = "pulse" ]; then
        _needs_pulse=1
        break
    fi
    _prev="$_arg"
done

if [ -x "${_bundled}" ] && [ "${_needs_pulse}" = "0" ]; then
    exec "${_bundled}" "$@"
fi

# Bundled binary not suitable -find system ffmpeg.
_appbin="${APPDIR:-}/usr/bin"
_sys_path="${PATH//${_appbin}:/}"
_sys_path="${_sys_path//${_appbin}/}"
_real="$(PATH="${_sys_path}" command -v "ffmpeg" 2>/dev/null)"

if [ -z "${_real}" ]; then
    printf '[FluxCast] ffmpeg: not found on this system.\n' >&2
    printf '[FluxCast] Install it with: sudo pacman -S ffmpeg\n' >&2
    exit 127
fi

exec "${_real}" "$@"
