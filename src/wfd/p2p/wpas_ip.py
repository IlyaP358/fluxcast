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
from pathlib import Path
from typing import Optional, Tuple

from ..config import WFDNotReady
from ..privileged import run_privileged, wrap_privileged
from .safety import safe_iface, safe_ipv4, safe_mac

# Standard Wi-Fi Direct convention subnet (matches diagnostics.py's own
# "P2P subnet" check, and Android's own WiFi Direct GO addressing) - used
# only when we end up as GO ourselves.
WFD_P2P_SUBNET = "192.168.49"

_go_dnsmasq_lease_file: Optional[str] = None


def _sudo_run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run *args* as root via the shared privileged helper (sudo / sudo -A)."""
    return run_privileged(args, timeout=timeout)


def parse_p2p_group_started_addrs(text: str) -> Optional[Tuple[str, str, str]]:
    """Parse ip_addr / ip_mask / go_ip_addr from a P2P-GROUP-STARTED line."""
    if "P2P-GROUP-STARTED" not in text:
        return None
    ip_m = re.search(r"\bip_addr=(\d+\.\d+\.\d+\.\d+)", text)
    mask_m = re.search(r"\bip_mask=(\d+\.\d+\.\d+\.\d+)", text)
    go_m = re.search(r"\bgo_ip_addr=(\d+\.\d+\.\d+\.\d+)", text)
    if not ip_m:
        return None
    return (
        ip_m.group(1),
        mask_m.group(1) if mask_m else "255.255.255.0",
        go_m.group(1) if go_m else "",
    )


def _p2p_ip_from_wpa_journal(iface: str) -> Optional[Tuple[str, str, str]]:
    try:
        journal = subprocess.run(
            [
                "journalctl", "-b", "--since", "2 min ago",
                "_COMM=wpa_supplicant", "--no-pager",
            ],
            capture_output=True, text=True, timeout=5.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in reversed((journal.stdout or "").splitlines()):
        if iface not in line:
            continue
        parsed = parse_p2p_group_started_addrs(line)
        if parsed:
            return parsed
    return None


def _mask_to_prefix(mask: str) -> int:
    try:
        parts = [int(p) for p in mask.split(".")]
        if len(parts) != 4:
            return 24
        n = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
        return bin(n).count("1")
    except ValueError:
        return 24


def _apply_static_ipv4(iface: str, ipv4: str, mask: str) -> None:
    prefix = _mask_to_prefix(mask)
    oper = ""
    try:
        oper = Path(f"/sys/class/net/{iface}/operstate").read_text().strip()
    except OSError:
        oper = ""
    # ip link set up on an already-associated client can DEAUTH_LEAVING.
    if oper != "up":
        _sudo_run(["ip", "link", "set", iface, "up"], timeout=5.0)
    _sudo_run(["ip", "addr", "replace", f"{ipv4}/{prefix}", "dev", iface], timeout=5.0)
    print(f"[FluxCast WFD] {iface} static P2P IPv4 {ipv4}/{prefix} (from GROUP-STARTED)")


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
                  physical_iface: Optional[str] = None,
                  *,
                  lease_timeout: float = 40.0) -> None:
    """Bring up a working IP on iface for the given P2P role.

    physical_iface (e.g. wlp0s20f3) is only used in the GO branch - some
    driver/firmware combinations demultiplex the projector's broadcast
    DHCPDISCOVER onto the physical radio instead of the P2P virtual
    interface, so dnsmasq needs to listen there too (see _run_as_go).
    """
    if role == "P2P-GO":
        _run_as_go(iface, peer_mac, physical_iface, timeout=lease_timeout)
        return
    # P2P-client: persistent groups often already negotiated 192.168.49.10.
    assigned = _p2p_ip_from_wpa_journal(iface)
    if assigned:
        ipv4, mask, go_ip = assigned
        _apply_static_ipv4(iface, ipv4, mask)
        if go_ip:
            try:
                _sudo_run(
                    ["ip", "neigh", "replace", go_ip, "nud", "reachable",
                     "dev", iface],
                    timeout=5.0,
                )
            except Exception:
                pass
        _strip_default_route(iface)
        return
    # Genuine DHCP from the GO. Do not nmcli-unmanage here — that bounces
    # carrier mid-DHCP. p2p-dev is unmanaged before Connect instead.
    _run_dhcp_client(iface, timeout=lease_timeout)


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

def _normalize_mac(mac: str) -> str:
    return mac.lower().replace("-", ":")


def _associated_sta_macs(iface: str) -> list[str]:
    """P2P *interface* addresses (often != the advertised device address)."""
    try:
        result = subprocess.run(
            ["iw", "dev", iface, "station", "dump"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    macs: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("Station "):
            continue
        mac = line.split()[1].strip()
        if re.fullmatch(r"[0-9a-fA-F:]{17}", mac):
            key = _normalize_mac(mac)
            if key not in macs:
                macs.append(key)
    return macs


def _dhcp_host_macs(iface: str, peer_mac: str) -> list[str]:
    macs: list[str] = []
    for raw in (peer_mac, *_associated_sta_macs(iface)):
        key = _normalize_mac(raw)
        if re.fullmatch(r"[0-9a-f:]{17}", key) and key not in macs:
            macs.append(key)
    return macs


def _macs_refused_in_log(log: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(
        r"DHCPDISCOVER\([^)]*\)\s+([0-9a-fA-F:]{17})\s+no address available",
        log,
        re.I,
    ):
        key = _normalize_mac(match.group(1))
        if key not in found:
            found.append(key)
    return found


def _iface_has_go_ip(iface: str) -> bool:
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", "dev", iface],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f"{WFD_P2P_SUBNET}.1/" in (result.stdout or "")


def _run_as_go(iface: str, peer_mac: str, physical_iface: Optional[str] = None,
                timeout: float = 40.0) -> None:
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
    lease_file = f"/tmp/fluxcast-dnsmasq-{iface}.leases"
    dnsmasq_log = f"/tmp/fluxcast-dnsmasq-{iface}.log"
    extra_now = _associated_sta_macs(iface)
    if _iface_has_go_ip(iface):
        existing = _wait_for_lease(
            lease_file,
            peer_mac,
            0.5,
            dnsmasq_log=dnsmasq_log,
            extra_macs=extra_now,
        )
        if existing:
            print(
                f"[FluxCast WFD] GO {iface} already serving {existing}; "
                "not restarting dnsmasq"
            )
            _prepare_go_rtsp_input(iface)
            _announce_go_ipv4(iface, gateway_ip)
            _seed_go_peer_neigh(iface, existing, peer_mac)
            subprocess.run(
                ["ping", "-c", "1", "-W", "1", "-I", iface, existing],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            return
    # Kill leftover dnsmasq *before* flushing the GO address. A stale
    # instance (previous attempt, missing dhcp-host) answers DISCOVER with
    # "no address available" and "packet received on p2p-0 which has no
    # address", and the sink gives up before the new daemon can ACK.
    _sudo_run(["pkill", "-f", f"dnsmasq.*{lease_file}"], timeout=5.0)
    _sudo_run(["pkill", "-f", f"dnsmasq.*--interface={iface}"], timeout=5.0)
    time.sleep(0.1)
    _sudo_run(["ip", "addr", "flush", "dev", iface], timeout=5.0)
    add = _sudo_run(["ip", "addr", "add", f"{gateway_ip}/24", "dev", iface], timeout=5.0)
    if add.returncode != 0:
        raise WFDNotReady(
            f"Could not assign {gateway_ip}/24 to {iface}: "
            f"{(add.stderr or add.stdout).strip()}"
        )
    _sudo_run(["ip", "link", "set", iface, "up"], timeout=5.0)
    # Sink SYNs 7236 immediately after DHCPACK (WFD). Do this *before*
    # dnsmasq so the first SYN is not racing INPUT DROP / rp_filter.
    _prepare_go_rtsp_input(iface)
    _announce_go_ipv4(iface, gateway_ip)
    if not _wait_for_link_running(iface, timeout=2.0):
        print(f"[FluxCast WFD] Warning: {iface} never reported LOWER_UP "
              "(carrier) - proceeding anyway, but this may be why dnsmasq "
              "fails to bind.")

    if not shutil.which("dnsmasq"):
        raise WFDNotReady("dnsmasq not found - needed to serve DHCP while we're P2P GO.")

    # Previous GO runs start dnsmasq via sudo; the lease file is root-owned.
    # os.remove then raises PermissionError and aborts after GROUP-STARTED.
    _sudo_run(["rm", "-f", lease_file], timeout=5.0)
    _sudo_run(["touch", lease_file], timeout=5.0)
    _sudo_run(["chmod", "a+rw", lease_file], timeout=5.0)
    _sudo_run(["rm", "-f", dnsmasq_log], timeout=5.0)
    _sudo_run(["touch", dnsmasq_log], timeout=5.0)
    _sudo_run(["chmod", "a+rw", dnsmasq_log], timeout=5.0)
    _go_dnsmasq_lease_file = lease_file

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
    # --dhcp-host sets the wfdsink tag on the sink's MAC. The range
    # must then require that tag (tag:) to actually restrict who's
    # eligible for it - set:wfdsink here would instead apply the tag
    # to whoever happens to take an address, which doesn't restrict
    # anything and would turn physical_iface into an open DHCP server
    # for every other device on that network.
    #
    # P2P device address (peer_mac) is not always the DHCP client
    # hardware address. P2P clients often DHCP from the *interface*
    # address (AP-STA-CONNECTED) which differs from the device address.
    host_macs = _dhcp_host_macs(iface, peer_mac)
    remaining = timeout
    peer_ip = None
    for attempt in range(2):
        host_args = [f"--dhcp-host={mac},set:wfdsink" for mac in host_macs]
        dnsmasq_cmd = ["dnsmasq", "--no-daemon", "--bind-dynamic",
                       f"--interface={iface}"]
        if physical_iface:
            # Parent STA radio: MAC-tag the range so we are not a rogue
            # DHCP server on the office LAN.
            dnsmasq_cmd.append(f"--interface={physical_iface}")
            range_arg = (
                f"--dhcp-range=tag:wfdsink,{WFD_P2P_SUBNET}.2,"
                f"{WFD_P2P_SUBNET}.254,255.255.255.0,1h"
            )
        else:
            # Dedicated P2P GO: only this BSS. Untagged range so the
            # sink's P2P *interface* MAC still gets a lease.
            range_arg = (
                f"--dhcp-range={WFD_P2P_SUBNET}.2,"
                f"{WFD_P2P_SUBNET}.254,255.255.255.0,1h"
            )
        dnsmasq_cmd += [
            *host_args,
            range_arg,
            f"--dhcp-option=3,{WFD_P2P_SUBNET}.1",
            f"--dhcp-option=6,{WFD_P2P_SUBNET}.1",
            f"--dhcp-leasefile={lease_file}",
            "--dhcp-authoritative",
            "--port=0",
            "--no-resolv", "--no-hosts",
        ]
        dnsmasq_cmd, dnsmasq_env = wrap_privileged(dnsmasq_cmd)
        extra = [m for m in host_macs if m != _normalize_mac(peer_mac)]
        extra_note = f" (also {', '.join(extra)})" if extra else ""
        print(f"[FluxCast WFD] Starting dnsmasq on {iface}{extra_note}...")
        log_fp = open(dnsmasq_log, "ab", buffering=0)
        subprocess.Popen(
            dnsmasq_cmd,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=dnsmasq_env,
        )
        print(
            f"[FluxCast WFD] Waiting up to {remaining:.0f}s for DHCP lease "
            f"from {peer_mac}..."
        )
        t0 = time.monotonic()
        peer_ip = _wait_for_lease(
            lease_file,
            peer_mac,
            remaining,
            dnsmasq_log=dnsmasq_log,
            extra_macs=extra,
        )
        remaining -= time.monotonic() - t0
        if peer_ip:
            break
        try:
            log = Path(dnsmasq_log).read_text(errors="replace")
        except OSError:
            log = ""
        refused = _macs_refused_in_log(log)
        new_macs = [m for m in refused if m not in host_macs]
        stale = bool(refused) and not new_macs and attempt == 0
        if remaining < 8 or not (new_macs or stale):
            break
        if new_macs:
            print(
                "[FluxCast WFD] DHCPDISCOVER from P2P interface MAC "
                f"{', '.join(new_macs)} (device addr {peer_mac}); "
                "restarting dnsmasq with that host"
            )
            host_macs.extend(new_macs)
        else:
            print(
                "[FluxCast WFD] Stale dnsmasq refused "
                f"{', '.join(refused)}; restarting"
            )
        _sudo_run(["pkill", "-f", f"dnsmasq.*{lease_file}"], timeout=5.0)
        _sudo_run(["pkill", "-f", f"dnsmasq.*--interface={iface}"], timeout=5.0)
        time.sleep(0.3)
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
    _prepare_go_rtsp_input(iface)
    _announce_go_ipv4(iface, gateway_ip)
    _seed_go_peer_neigh(iface, peer_ip, peer_mac)
    subprocess.run(
        ["ping", "-c", "1", "-W", "1", "-I", iface, peer_ip],
        capture_output=True,
        text=True,
        timeout=3.0,
    )


def _announce_go_ipv4(iface: str, ipv4: str) -> None:
    """Gratuitous ARP so the sink can unicast RTSP to the GO after DHCPACK.

    DHCP is UDP broadcast; RTSP is TCP to the GO IPv4. Without a GARP the
    sink's ARP for the gateway/WFD Local IP can miss the 10s post-lease
    window and the STA deauths (reason 3) with no M1.
    """
    iface = safe_iface(iface)
    ipv4 = safe_ipv4(ipv4)
    if not iface or not ipv4:
        return
    _sudo_run(
        ["sysctl", "-w", f"net.ipv4.conf.{iface}.arp_notify=1"],
        timeout=3.0,
    )
    _sudo_run(
        ["ip", "addr", "change", f"{ipv4}/24", "dev", iface],
        timeout=3.0,
    )
    if shutil.which("arping"):
        _sudo_run(
            ["arping", "-c", "2", "-U", "-I", iface, ipv4],
            timeout=5.0,
        )


def _prepare_go_rtsp_input(iface: str) -> None:
    """Accept WFD RTSP on this GO iface before the sink's first SYN."""
    iface = safe_iface(iface)
    if not iface:
        return
    _sudo_run(
        ["sysctl", "-w", f"net.ipv4.conf.{iface}.rp_filter=0"],
        timeout=3.0,
    )
    ins = _sudo_run(
        [
            "iptables", "-C", "INPUT", "-i", iface, "-p", "tcp",
            "--dport", "7236", "-j", "ACCEPT",
        ],
        timeout=3.0,
    )
    if ins.returncode != 0:
        _sudo_run(
            [
                "iptables", "-I", "INPUT", "1", "-i", iface, "-p", "tcp",
                "--dport", "7236", "-j", "ACCEPT",
            ],
            timeout=3.0,
        )
    _sudo_run(
        [
            "ufw", "allow", "in", "on", iface, "to", "any", "port", "7236",
            "proto", "tcp",
        ],
        timeout=3.0,
    )


def _seed_go_peer_neigh(iface: str, peer_ip: str, peer_mac: str) -> None:
    """Install a neighbour so SYN-ACK to the sink is not waiting on ARP."""
    iface = safe_iface(iface)
    peer_ip = safe_ipv4(peer_ip)
    if not iface or not peer_ip:
        return
    macs = [m for m in _dhcp_host_macs(iface, peer_mac) if safe_mac(m)]
    for mac in macs:
        _sudo_run(
            [
                "ip", "neigh", "replace", peer_ip, "lladdr", mac,
                "nud", "reachable", "dev", iface,
            ],
            timeout=3.0,
        )


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


def _wait_for_lease(
    lease_file: str,
    peer_mac: str,
    timeout: float,
    dnsmasq_log: Optional[str] = None,
    extra_macs: Optional[list[str]] = None,
) -> Optional[str]:
    targets = {_normalize_mac(peer_mac)}
    for mac in extra_macs or []:
        targets.add(_normalize_mac(mac))
    ack_re = re.compile(
        r"DHCPACK\([^)]*\)\s+(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:.-]{17})",
        re.I,
    )
    deadline = time.monotonic() + timeout
    _sudo_run(["chmod", "a+rw", lease_file], timeout=3.0)
    while time.monotonic() < deadline:
        try:
            with open(lease_file) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mac = _normalize_mac(parts[1])
                    ip = parts[2]
                    if mac in targets or ip.startswith(WFD_P2P_SUBNET + "."):
                        return ip
        except OSError:
            cat = _sudo_run(["cat", lease_file], timeout=3.0)
            for line in (cat.stdout or "").splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                mac = _normalize_mac(parts[1])
                ip = parts[2]
                if mac in targets or ip.startswith(WFD_P2P_SUBNET + "."):
                    return ip
        if dnsmasq_log:
            try:
                log = Path(dnsmasq_log).read_text(errors="replace")
            except OSError:
                log = ""
            for match in ack_re.finditer(log):
                mac = _normalize_mac(match.group(2))
                ip = match.group(1)
                if mac in targets or ip.startswith(WFD_P2P_SUBNET + "."):
                    return ip
        if timeout <= 0.5:
            break
        time.sleep(0.4)
    return None


def _stop_go_dnsmasq() -> None:
    global _go_dnsmasq_lease_file
    try:
        if _go_dnsmasq_lease_file:
            _sudo_run(["pkill", "-f", f"dnsmasq.*{_go_dnsmasq_lease_file}"], timeout=5.0)
        _sudo_run(["pkill", "-f", "dnsmasq.*fluxcast-dnsmasq"], timeout=5.0)
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
        oper = ""
        try:
            oper = Path(f"/sys/class/net/{iface}/operstate").read_text().strip()
        except OSError:
            oper = ""
        if oper != "up":
            try:
                _sudo_run(["ip", "link", "set", iface, "up"], timeout=5.0)
            except Exception as exc:
                print(f"[FluxCast WFD] Warning: ip link set {iface} up: {exc}")
        try:
            result = _sudo_run(["dhcpcd", "-G", iface], timeout=timeout + 10.0)
        except subprocess.TimeoutExpired as exc:
            raise WFDNotReady(
                f"dhcpcd timed out on {iface} (no carrier/lease). "
                "P2P-client netdev was likely deauthed (DEAUTH_LEAVING)."
            ) from exc
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


def is_p2p_split_iface(iface: str) -> bool:
    """P2P netdevs NM must not own (see meta/99-fluxcast-p2p.conf)."""
    return bool(iface) and iface.startswith("p2p-")


def mark_unmanaged(iface: str) -> None:
    """Keep NetworkManager off a P2P iface. Never call this on the STA uplink."""
    if not iface or not is_p2p_split_iface(iface):
        print(
            f"[FluxCast WFD] Refusing to unmanage {iface!r} "
            "(STA stays with NetworkManager)"
        )
        return
    try:
        _sudo_run(["nmcli", "device", "set", iface, "managed", "no"], timeout=5.0)
    except Exception as exc:
        print(f"[FluxCast WFD] Warning: could not mark {iface} unmanaged: {exc}")


def mark_managed(iface: str) -> None:
    """Hand a *non-P2P* iface back to NetworkManager.

    p2p-dev-* / p2p-* stay unmanaged (NM STA / wpas P2P split). Remanaging
    them is how GroupStarted became unexpected and DEAUTH_LEAVING fired.
    """
    if is_p2p_split_iface(iface):
        return
    try:
        _sudo_run(["nmcli", "device", "set", iface, "managed", "yes"], timeout=5.0)
    except Exception as exc:
        print(f"[FluxCast WFD] Warning: could not mark {iface} managed: {exc}")
