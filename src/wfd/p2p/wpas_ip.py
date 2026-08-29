"""IP-layer configuration for the raw-wpa_supplicant P2P backend (wpas.py).

Split out of wpas.py to keep each file focused. This module picks up once a
P2P group already exists: which role we ended up with, and how to get a
working IP on the resulting interface for that role.

The P2P spec doesn't guarantee who becomes Group Owner - it's negotiated,
and plenty of real-world WFD sinks expect to end up as GO themselves while
the source acts as client. When the roles land the other way around (we're
GO, the sink is client), a plain DHCP client on our side has nothing to
talk to, and NetworkManager's own path correctly self-assigns an address
with no gateway rather than pretending otherwise.

So this module detects the actual role (get_p2p_role) and branches: as GO,
serve DHCP to the sink ourselves (_run_as_go: static IP + dnsmasq); as
client, run a real DHCP client (_run_dhcp_client) against the sink's own
DHCP server.
"""

import os
import re
import shutil
import subprocess
import time
from typing import Optional

from ..config import WFDNotReady

# Standard Wi-Fi Direct convention subnet (matches diagnostics.py's own
# "P2P subnet" check, and Android's own WiFi Direct GO addressing) - used
# only when we end up as GO ourselves.
WFD_P2P_SUBNET = "192.168.49"

_go_dnsmasq_lease_file: Optional[str] = None


def _sudo_run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but under sudo. This whole backend runs as the
    calling user (main.py is never invoked with sudo itself) - binding a DHCP
    client/server to a raw interface and touching routes both need root.
    sudo's password prompt goes straight to the controlling terminal, so this
    stays interactive if the session's cached sudo timestamp has expired.
    """
    return subprocess.run(["sudo", *args], capture_output=True, text=True, timeout=timeout)


def get_p2p_role(iface: str) -> str:
    """Returns 'P2P-GO', 'P2P-client', or 'unknown'."""
    result = subprocess.run(["iw", "dev", iface, "info"],
                             capture_output=True, text=True, timeout=5.0)
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("type"):
            return line.partition(" ")[2].strip()
    return "unknown"


def configure_ip(iface: str, peer_mac: str, role: str,
                  physical_iface: Optional[str] = None) -> None:
    """Bring up a working IP on iface for the given P2P role.

    physical_iface (e.g. wlp0s20f3) is only used in the GO branch - some
    driver/firmware combinations demultiplex the projector's broadcast
    DHCPDISCOVER onto the physical radio instead of the P2P virtual
    interface, so dnsmasq needs to listen there too (see _run_as_go).
    """
    if role == "P2P-GO":
        _run_as_go(iface, peer_mac, physical_iface)
    else:
        # Genuine P2P-client role: a real DHCP server (the projector,
        # correctly acting as GO) should be on the other end. Deliberately
        # not marking the interface NM-unmanaged here - telling NM to
        # release a device it just discovered (rather than never managing
        # it via a static conf.d rule matched at interface-creation time)
        # was observed bouncing the interface down/up ("carrier lost"
        # mid-DHCP on an earlier run).
        _run_dhcp_client(iface)


def release_ip_config(iface: str) -> None:
    """Undo configure_ip(): stop whatever we started (dnsmasq or a DHCP
    client) and hand the interface back to NetworkManager.
    """
    _stop_go_dnsmasq()  # no-op if we were P2P-client, not GO

    release_cmd = None
    if shutil.which("dhcpcd"):
        release_cmd = ["dhcpcd", "-k", iface]
    elif shutil.which("dhclient"):
        release_cmd = ["dhclient", "-r", iface]
    if release_cmd:
        try:
            _sudo_run(release_cmd, timeout=10.0)
        except Exception as exc:
            print(f"[FluxCast WFD] Warning: DHCP release failed on {iface}: {exc}")

    try:
        _sudo_run(["nmcli", "device", "set", iface, "managed", "yes"], timeout=5.0)
    except Exception as exc:
        print(f"[FluxCast WFD] Warning: could not restore NM management of {iface}: {exc}")


# ---------------------------------------------------------------------------
# GO role: we host DHCP for the projector.
# ---------------------------------------------------------------------------

def _run_as_go(iface: str, peer_mac: str, physical_iface: Optional[str] = None,
                timeout: float = 25.0) -> None:
    global _go_dnsmasq_lease_file
    gateway_ip = f"{WFD_P2P_SUBNET}.1"
    print(f"[FluxCast WFD] We are P2P Group Owner; assigning ourselves {gateway_ip}/24 "
          "and serving DHCP to the projector...")

    # Unlike the client-role path (see configure_ip's comment on why it
    # skips this), marking the interface unmanaged here protects against
    # NetworkManager auto-detecting the new P2P interface and activating its
    # own "shared" mode (self-assign + its own dnsmasq) on it concurrently,
    # which would conflict with the dnsmasq instance we're about to start.
    mark_unmanaged(iface)
    _sudo_run(["ip", "addr", "flush", "dev", iface], timeout=5.0)
    add = _sudo_run(["ip", "addr", "add", f"{gateway_ip}/24", "dev", iface], timeout=5.0)
    if add.returncode != 0:
        raise WFDNotReady(
            f"Could not assign {gateway_ip}/24 to {iface}: "
            f"{(add.stderr or add.stdout).strip()}"
        )
    _sudo_run(["ip", "link", "set", iface, "up"], timeout=5.0)
    if not _wait_for_link_running(iface, timeout=10.0):
        print(f"[FluxCast WFD] Warning: {iface} never reported LOWER_UP "
              "(carrier) - proceeding anyway, but this may be why dnsmasq "
              "fails to bind.")

    if not shutil.which("dnsmasq"):
        raise WFDNotReady("dnsmasq not found - needed to serve DHCP while we're P2P GO.")

    lease_file = f"/tmp/fluxcast-dnsmasq-{iface}.leases"
    try:
        os.remove(lease_file)
    except FileNotFoundError:
        pass
    _go_dnsmasq_lease_file = lease_file
    dnsmasq_log = f"/tmp/fluxcast-dnsmasq-{iface}.log"

    # dnsmasq is scoped by the sink's MAC address (--dhcp-host + a tagged
    # --dhcp-range) rather than by interface. This matters for two reasons:
    #
    # - Some driver/firmware combinations deliver the sink's DHCPDISCOVER
    #   broadcast on the physical radio (physical_iface) rather than the
    #   P2P virtual interface, even though the P2P link is what's actually
    #   carrying it - so dnsmasq needs to listen on both.
    # - physical_iface also carries our regular office Wi-Fi traffic, so
    #   serving DHCP there unconditionally would make this laptop a rogue
    #   DHCP server for every other device on that network. Scoping by MAC
    #   means we only ever answer this one specific sink, on whichever
    #   interface its broadcast happens to land on.
    #
    # Note that listing an interface explicitly disables --bind-dynamic's
    # usual auto-detection for any *other* interface, so once physical_iface
    # is involved at all, both interfaces need to be named explicitly.
    dnsmasq_cmd = ["sudo", "dnsmasq", "--no-daemon", "--bind-dynamic",
                   f"--interface={iface}"]
    if physical_iface:
        dnsmasq_cmd.append(f"--interface={physical_iface}")
    dnsmasq_cmd += [
        f"--dhcp-host={peer_mac},set:wfdsink",
        f"--dhcp-range=set:wfdsink,{WFD_P2P_SUBNET}.2,{WFD_P2P_SUBNET}.254,255.255.255.0,1h",
        f"--dhcp-leasefile={lease_file}",
        "--port=0",  # DHCP only, no DNS service on this interface
        "--no-resolv", "--no-hosts",
    ]
    print(f"[FluxCast WFD] Starting dnsmasq on {iface}...")
    with open(dnsmasq_log, "wb") as log_fp:
        # We don't track this via the returned Popen's .pid: depending on
        # sudoers pty settings, sudo may exec-replace itself (pid stays the
        # same) or fork-and-monitor (it doesn't), so the pid isn't reliably
        # dnsmasq's own. release_ip_config matches on the lease-file path
        # instead (see _stop_go_dnsmasq) - that's also why cleaning up after
        # a failed run matters: a lingering instance would otherwise
        # port-conflict with the next one.
        subprocess.Popen(dnsmasq_cmd, stdout=log_fp, stderr=subprocess.STDOUT)

    peer_ip = _wait_for_lease(lease_file, peer_mac, timeout)
    if not peer_ip:
        try:
            with open(dnsmasq_log) as f:
                print(f"[FluxCast WFD] dnsmasq log:\n{f.read()}")
        except FileNotFoundError:
            pass
        raise WFDNotReady(
            f"No DHCP lease handed out to {peer_mac} after {timeout:.0f}s - "
            "the projector may not be attempting DHCP client behaviour "
            "(some WFD sink firmware assumes it will always be GO and may "
            "not have real client-mode network logic to fall back on)."
        )
    print(f"[FluxCast WFD] Projector leased {peer_ip}. Seeding ARP entry...")
    subprocess.run(["ping", "-c", "1", "-W", "2", "-I", iface, peer_ip],
                    capture_output=True, text=True, timeout=5.0)


def _wait_for_link_running(iface: str, timeout: float) -> bool:
    """`ip link set up` returning doesn't guarantee the kernel/driver has
    actually brought up carrier (LOWER_UP) on a freshly created P2P virtual
    interface yet - dnsmasq can fail to bind if it starts too early. Poll
    for LOWER_UP instead of assuming the interface is ready immediately.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["ip", "link", "show", iface],
                                 capture_output=True, text=True, timeout=3.0)
        if "LOWER_UP" in result.stdout:
            return True
        time.sleep(0.5)
    return False


def _wait_for_lease(lease_file: str, peer_mac: str, timeout: float) -> Optional[str]:
    target = peer_mac.lower()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(lease_file) as f:
                for line in f:
                    # dnsmasq.leases format: <expiry> <mac> <ip> <hostname> <client-id>
                    parts = line.split()
                    if len(parts) >= 3 and parts[1].lower() == target:
                        return parts[2]
        except FileNotFoundError:
            pass
        time.sleep(1)
    return None


def _stop_go_dnsmasq() -> None:
    global _go_dnsmasq_lease_file
    if _go_dnsmasq_lease_file is None:
        return
    try:
        _sudo_run(["pkill", "-f", f"dnsmasq.*{_go_dnsmasq_lease_file}"], timeout=5.0)
    except Exception as exc:
        print(f"[FluxCast WFD] Warning: could not stop dnsmasq: {exc}")
    _go_dnsmasq_lease_file = None


# ---------------------------------------------------------------------------
# Client role: the projector hosts DHCP, we request a lease from it.
# ---------------------------------------------------------------------------

def _run_dhcp_client(iface: str, timeout: float = 20.0) -> None:
    print(f"[FluxCast WFD] Requesting DHCP lease on {iface} directly (bypassing NM)...")
    if shutil.which("dhcpcd"):
        # No -1 (one-shot): a one-shot run exits immediately once configured,
        # leaving nothing for the later `dhcpcd -U` call (_seed_peer_arp_entry)
        # to query. Running persistently keeps a daemon alive to ask about
        # the lease's gateway afterward. It also means this call blocks in
        # the foreground until the lease actually completes (that's what
        # triggers its fork-to-background) rather than returning early, so
        # it needs the full timeout budget, not a short launch-only one.
        # -G: never install a default route from this lease. This link is to
        # an external, untrusted P2P peer (the sink) - it must never become
        # the machine's route to the internet/office LAN.
        result = _sudo_run(["dhcpcd", "-G", iface], timeout=timeout + 10.0)
        if result.returncode != 0:
            raise WFDNotReady(
                f"dhcpcd failed to start on {iface}: {(result.stderr or result.stdout).strip()}"
            )
        if not _wait_for_ipv4(iface, timeout):
            raise WFDNotReady(f"No IPv4 address on {iface} after {timeout:.0f}s.")
    elif shutil.which("dhclient"):
        # dhclient has no no-default-route flag; strip any route it installs
        # immediately after. One-shot mode is fine here since
        # _seed_peer_arp_entry only tries dhcpcd -U.
        result = _sudo_run(["dhclient", "-1", "-v", iface], timeout=timeout + 5.0)
        if result.returncode != 0:
            raise WFDNotReady(
                f"dhclient failed on {iface}: {(result.stderr or result.stdout).strip()}"
            )
    else:
        raise WFDNotReady(
            "No DHCP client found (checked dhcpcd, dhclient). "
            "The P2P data interface needs its own DHCP client since "
            "NetworkManager is not managing it."
        )
    _seed_peer_arp_entry(iface)
    _strip_default_route(iface)


def _wait_for_ipv4(iface: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["ip", "-4", "-br", "addr", "show", "dev", iface],
                                 capture_output=True, text=True, timeout=3.0)
        if result.returncode == 0 and re.search(r"\d+\.\d+\.\d+\.\d+/\d+", result.stdout):
            return True
        time.sleep(1)
    return False


def _seed_peer_arp_entry(iface: str) -> None:
    """Ping the DHCP-lease gateway once so the kernel resolves its ARP entry.

    The projector is the DHCP server here (it's the P2P Group Owner), so the
    lease's gateway/router option IS the projector's IP - but -G means it
    was never installed as a route, so it can't be read back from `ip route`.
    `dhcpcd -U` dumps the raw lease options regardless of -G. Without this,
    FluxCast's active RTSP probe (probe.py -> addressing.py) finds an empty
    ARP table and never discovers the sink's address, because that lookup
    only reads the existing neighbour cache - it never triggers resolution
    itself. This ping is what NetworkManager's own connectivity checks did
    for free on the nm.py path.
    """
    if not shutil.which("dhcpcd"):
        return
    result = _sudo_run(["dhcpcd", "-U", iface], timeout=5.0)
    if result.returncode != 0:
        return
    gateway = next(
        (line.partition("=")[2].strip().strip("'\"")
         for line in result.stdout.splitlines() if line.startswith("routers=")),
        None,
    )
    if not gateway:
        print("[FluxCast WFD] Warning: no gateway in DHCP lease; "
              "active RTSP probe may not find the sink's IP.")
        return
    print(f"[FluxCast WFD] Seeding ARP entry for sink at {gateway}...")
    subprocess.run(["ping", "-c", "1", "-W", "2", "-I", iface, gateway],
                    capture_output=True, text=True, timeout=5.0)


def _strip_default_route(iface: str) -> None:
    """Defense in depth on top of dhcpcd -G: remove any default route the
    lease installed on this interface. The peer is an external, untrusted
    device - it must never become this machine's route to the internet or
    the office LAN, regardless of which DHCP client handled the lease.
    """
    try:
        _sudo_run(["ip", "route", "del", "default", "dev", iface], timeout=5.0)
    except Exception:
        pass


def mark_unmanaged(iface: str) -> None:
    """Keep NetworkManager off the interface we're about to hand-configure.

    Used in the GO branch (see _run_as_go); the client branch deliberately
    skips this - see configure_ip's comment for why. A static conf.d
    unmanaged-devices rule would be a cleaner long-term replacement for
    this reactive nmcli call, if that turns out to matter in practice.
    """
    try:
        _sudo_run(["nmcli", "device", "set", iface, "managed", "no"], timeout=5.0)
    except Exception as exc:
        print(f"[FluxCast WFD] Warning: could not mark {iface} unmanaged: {exc}")
