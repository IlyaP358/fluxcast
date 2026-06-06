#!/bin/bash
_tool="$(basename "$0")"
_appbin="${APPDIR:-}/usr/bin"

# Find the real binary by excluding AppDir/usr/bin from PATH
_sys_path="${PATH//${_appbin}:/}"
_sys_path="${_sys_path//${_appbin}/}"
_real="$(PATH="${_sys_path}" command -v "${_tool}" 2>/dev/null)"

if [ -z "${_real}" ]; then
    printf '[FluxCast] %s: not found on this system (install it and retry)\n' "${_tool}" >&2
    exit 127
fi

if [ -n "${APPDIR:-}" ]; then
    _ld=""
    IFS=':' read -ra _parts <<< "${LD_LIBRARY_PATH:-}"
    for _p in "${_parts[@]}"; do
        [[ "$_p" == "${APPDIR}"* ]] || _ld+="${_ld:+:}${_p}"
    done
    export LD_LIBRARY_PATH="${_ld}"
    unset LD_PRELOAD
fi

exec "${_real}" "$@"
