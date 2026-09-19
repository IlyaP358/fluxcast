"""Single privileged-exec helper for WFD (sudo timestamp + graphical askpass).

Call sites must not invent their own ``sudo`` argv. This module:

* runs the command as-is when already root
* uses ``sudo`` when stdin is a TTY (default 5 minute timestamp)
* uses ``sudo -A`` plus an askpass program when stdin is not a TTY
  (tray / panel / piped logs) for the first prompt
* after a successful prime, later calls in this process use ``sudo -n``
  so askpass is not invoked again (``sudo -A`` can re-prompt even with a
  ticket; 5s USB timeouts then kill the dialog)

A Hyprland or panel process can often open ``/dev/tty`` (the session TTY)
even when stdin is a pipe. That is not a prompt the user can see, and with
``capture_output=True`` sudo fails immediately with "no password was
provided". Do not treat ``/dev/tty`` as interactive.

wpa_supplicant has no polkit; NetworkManager does. Do not wrap NM D-Bus
in this helper.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import WFDNotReady

_ASKPASS_NAMES = (
    "ssh-askpass",
    "x11-ssh-askpass",
    "lxqt-openssh-askpass",
    "ksshaskpass",
    "ksshaskpass5",
    "wayland-askpass",
)

# sudo -A has no terminal, so the default timestamp_type=tty behaves like
# ppid: each new FluxCast/scan/ctl process is a new ticket. One per-user
# record (8 minutes) matches the GUI reconnect flow.
_SUDOERS_DROPIN = Path("/etc/sudoers.d/10-fluxcast-timestamp")
_SUDOERS_BODY = (
    "# Managed by FluxCast. GUI/panel sudo -A has no TTY, so tty tickets\n"
    "# never reuse a timestamp across scan/start/reconnect.\n"
    "Defaults timestamp_type=global\n"
    "Defaults timestamp_timeout=8\n"
)

# Set by prime_privileged after a successful sudo -v / sudo -A -v.
_ticket_ok = False


def _clean_sudo_err(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        if "PyGIWarning" in line or "DeprecationWarning" in line:
            continue
        if line.startswith("<stdin>:"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _stdin_is_tty() -> bool:
    try:
        return os.isatty(0)
    except OSError:
        return False


def _bundled_askpass() -> Optional[str]:
    here = Path(__file__).resolve()
    # src/wfd/privileged.py → repo meta/ or /opt/fluxcast sibling.
    candidates = (
        here.parents[2] / "meta" / "fluxcast-askpass",
        Path("/usr/libexec/fluxcast-askpass"),
        Path("/usr/lib/fluxcast/askpass"),
    )
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _askpass_path() -> Optional[str]:
    for key in ("FLUXCAST_ASKPASS", "SUDO_ASKPASS"):
        value = os.environ.get(key, "").strip()
        if value and os.path.isfile(value) and os.access(value, os.X_OK):
            return value
    bundled = _bundled_askpass()
    if bundled:
        return bundled
    for name in _ASKPASS_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def wrap_privileged(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Return argv and env to exec *argv* as root.

    Does not prepend sudo when already root or when argv already starts
    with sudo.
    """
    env = os.environ.copy()
    if not argv:
        return argv, env
    if os.geteuid() == 0 or argv[0] == "sudo":
        return list(argv), env
    if _ticket_ok:
        return ["sudo", "-n", *argv], env
    if _stdin_is_tty():
        return ["sudo", *argv], env
    ask = _askpass_path()
    if ask:
        env["SUDO_ASKPASS"] = ask
        return ["sudo", "-A", *argv], env
    return ["sudo", *argv], env


def _sudo_run(
    cmd: list[str],
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env,
    }
    if cmd[:2] == ["sudo", "-A"] or cmd[:2] == ["sudo", "-n"]:
        # Inherited stdin is a pipe from the panel; do not let sudo treat
        # it as a password source.
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(cmd, **kwargs)


def _askpass_wrap(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    ask = _askpass_path()
    if ask:
        env["SUDO_ASKPASS"] = ask
        return ["sudo", "-A", *argv], env
    return ["sudo", *argv], env


def run_privileged(
    argv: list[str],
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run *argv* as root. After prime, sudo -n; else sudo / sudo -A."""
    cmd, env = wrap_privileged(argv)
    run_timeout = timeout
    if cmd[:2] == ["sudo", "-A"]:
        run_timeout = max(timeout, 120.0)
    result = _sudo_run(cmd, env, run_timeout)
    err = (result.stderr or result.stdout or "")
    if (
        result.returncode != 0
        and cmd[:2] == ["sudo", "-n"]
        and ("password" in err.lower() or "a password is required" in err.lower())
    ):
        cmd, env = _askpass_wrap(argv)
        result = _sudo_run(cmd, env, max(timeout, 120.0))
    return result


def _ensure_sudo_timestamp_policy() -> None:
    """Install a sudoers drop-in so sudo -A timestamps are per-user, not per-pid."""
    try:
        existing = _SUDOERS_DROPIN.read_text(encoding="utf-8")
        if "timestamp_type=global" in existing:
            return
    except OSError:
        pass
    tmp = Path("/tmp/fluxcast-timestamp.sudoers")
    try:
        tmp.write_text(_SUDOERS_BODY, encoding="utf-8")
    except OSError:
        return
    chk = subprocess.run(
        ["visudo", "-c", "-f", str(tmp)],
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    if chk.returncode != 0:
        return
    # After prime the ticket should already be valid. Never pop a second
    # askpass (or 10s-timeout the dialog) during connect.
    cmd, env = wrap_privileged(
        ["install", "-m", "0440", "-o", "root", "-g", "root", str(tmp), str(_SUDOERS_DROPIN)]
    )
    if cmd[:1] == ["sudo"]:
        if cmd[1:2] == ["-A"]:
            cmd = ["sudo", "-A", "-n", *cmd[2:]]
        elif "-n" not in cmd[:3]:
            cmd = ["sudo", "-n", *cmd[1:]]
    installed = _sudo_run(cmd, env, 8.0)
    if installed.returncode != 0:
        print(
            "[FluxCast WFD] Warning: could not install sudo timestamp policy: "
            f"{_clean_sudo_err(installed.stderr or installed.stdout or '')}"
        )


def prime_privileged(timeout: float = 120.0) -> None:
    """Cache a sudo timestamp; graphical askpass if stdin is not a TTY."""
    if os.geteuid() == 0:
        return
    if not shutil.which("sudo"):
        raise WFDNotReady(
            "sudo is required for Wi-Fi Direct (ip, dnsmasq, wpa_supplicant)."
        )
    cmd, env = wrap_privileged(["true"])
    # sudo -v only refreshes the ticket; use it when wrapping produced sudo.
    if cmd[:1] == ["sudo"]:
        if cmd[1:2] == ["-A"]:
            cmd = ["sudo", "-A", "-v"]
        else:
            cmd = ["sudo", "-v"]
    try:
        result = _sudo_run(cmd, env, timeout)
    except subprocess.TimeoutExpired as exc:
        raise WFDNotReady(
            "Timed out waiting for administrator password (sudo)."
        ) from exc
    except OSError as exc:
        raise WFDNotReady(f"Failed to run sudo: {exc}") from exc
    if result.returncode != 0:
        err = _clean_sudo_err(result.stderr or result.stdout or "")
        raise WFDNotReady(
            "Administrator authentication failed (sudo): "
            f"{err or 'password required'}"
        )
    global _ticket_ok
    _ticket_ok = True
    _ensure_sudo_timestamp_policy()
