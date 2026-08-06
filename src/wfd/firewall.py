import shutil
import subprocess

from .proc import _run


def _firewalld_active() -> bool:
    if not shutil.which("firewall-cmd"):
        return False
    try:
        result = _run(["systemctl", "is-active", "firewalld"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "active"

_FIREWALL_AUTH_TIMEOUT = 60.0

_FIREWALL_QUERY_TIMEOUT = 3.0

_WFD_FIREWALL_ZONE = "nm-shared"

def _print_firewall_manual_hint(port: int, reason: str) -> None:
    print(
        f"[FluxCast WFD] Could not open firewalld port {port}/tcp automatically "
        f"({reason}).\n"
        "  Open it once yourself, then re-run FluxCast:\n"
        f"    sudo firewall-cmd --permanent --zone={_WFD_FIREWALL_ZONE} --add-port={port}/tcp "
        "&& sudo firewall-cmd --reload\n"
        "  or pass --wfd-no-firewall if you manage the firewall yourself."
    )

def _open_wfd_firewall_port(port: int) -> bool:
    """
    Runtime-only (no ``--permanent``): cleared on reload/reboot, and removed on
    exit. Returns True only if WE opened it, so the caller knows to undo it; a
    port the user already had open is left untouched.
    """
    if not _firewalld_active():
        return False

    try:
        query = _run(
            [
                "firewall-cmd",
                f"--zone={_WFD_FIREWALL_ZONE}",
                f"--query-port={port}/tcp",
            ],
            timeout=_FIREWALL_QUERY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _print_firewall_manual_hint(port, f"could not check existing rule: {exc}")
        return False

    # A real query result is the literal ``yes``/``no`` output. Authorization
    # failures can use the same non-zero status as a closed port, so do not
    # infer the result from the return code alone.
    query_status = query.stdout.strip().lower()
    query_output = (query.stdout + query.stderr).strip()
    if query_status == "yes":
        return False  # The user already had it open; leave it untouched.
    if query_status != "no":
        _print_firewall_manual_hint(
            port,
            query_output or "firewall-cmd could not verify the existing rule",
        )
        return False

    print(f"[FluxCast WFD] Opening firewalld port {port}/tcp ({_WFD_FIREWALL_ZONE} zone) "
          "for this session; approve the authorization prompt if one appears.")
    try:
        result = _run(["firewall-cmd", f"--zone={_WFD_FIREWALL_ZONE}",
                       f"--add-port={port}/tcp"],
                      timeout=_FIREWALL_AUTH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _print_firewall_manual_hint(port, f"authorization did not complete: {exc}")
        return False
    output = result.stdout + result.stderr
    if result.returncode != 0:
        _print_firewall_manual_hint(port, output.strip() or "firewall-cmd refused the request")
        return False
    if "ALREADY_ENABLED" in output.upper():
        return False  # user already had it open; leave it exactly as-is
    print(f"[FluxCast WFD] Opened firewalld port {port}/tcp for this session "
          "(removed on exit).")
    return True

def _close_wfd_firewall_port(port: int) -> None:
    """Undo _open_wfd_firewall_port (best-effort)."""
    try:
        _run(["firewall-cmd", f"--zone={_WFD_FIREWALL_ZONE}",
              f"--remove-port={port}/tcp"], timeout=15.0)
        print(f"[FluxCast WFD] Closed firewalld port {port}/tcp.")
    except (OSError, subprocess.TimeoutExpired):
        pass
