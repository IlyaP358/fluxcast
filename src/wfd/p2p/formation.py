"""Hardware-agnostic Wi-Fi Direct group-formation helpers.

These helpers encode WFD/P2P practices (peer listen channel, listen-then
provision-discovery before GO Neg, finished-group vs pending-GO criteria
in log text). They are not tied to a chip family. Callers supply their own
wpa_cli binding (dedicated ctrl socket, system wpa, etc.).
"""

from __future__ import annotations

import re
import time
from typing import Callable, Mapping

# wpa_cli-shaped callable: (*args) -> object with .returncode / .stdout / .stderr
WpaCli = Callable[..., object]


def parse_p2p_peer_info(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def peer_listen_freq(
    peer_info: Mapping[str, str], default: int = 2437
) -> int:
    """Peer listen_freq from p2p_peer fields, else default social ch6."""
    raw = peer_info.get("listen_freq", "")
    try:
        freq = int(raw)
        if 2400 <= freq <= 6000:
            return freq
    except ValueError:
        pass
    return default


def freq_to_social_channel(freq: int) -> tuple[str, str]:
    """Map common 2.4 GHz MHz → (reg_class, channel) for p2p listen/oper."""
    ch = {2412: "1", 2437: "6", 2462: "11"}.get(freq, "6")
    return "81", ch


def parse_iw_phy_caps(iw_phy_text: str) -> dict[str, bool]:
    """Parse `iw phy` text: what *this* radio can advertise, not a chip table."""
    t = iw_phy_text or ""
    has_5ghz = bool(
        re.search(r"\b5[0-9]{3}\.0 MHz\b", t) or "Band 2:" in t
    )
    has_2ghz = bool(re.search(r"\b24[0-9]{2}\.0 MHz\b", t) or "Band 1:" in t)
    has_vht = "VHT Capabilities" in t
    has_vht80 = has_vht and (
        "80 MHz" in t or "short GI (80 MHz)" in t or "[80 MHz" in t
    )
    has_ht40 = "40 MHz" in t or "HT40" in t
    return {
        "has_2ghz": has_2ghz,
        "has_5ghz": has_5ghz,
        "has_ht40": has_ht40,
        "has_vht": has_vht,
        "has_vht80": has_vht80,
    }


def peer_advertises_5ghz(peer_info: Mapping[str, str]) -> bool:
    """True if this P2P peer (any sink) lists a 5 GHz listen/oper/channel."""
    for key in ("oper_freq", "listen_freq", "freq"):
        try:
            freq = int(peer_info.get(key) or 0)
        except ValueError:
            continue
        if 5000 <= freq <= 6000:
            return True
    blob = " ".join(
        str(peer_info.get(k) or "")
        for k in ("channels", "channel_list", "freq_list")
    )
    if re.search(r"\b(11[5-9]|12[0-7])\b", blob):
        return True
    if re.search(r"\b(36|40|44|48|149|153|157|161|165)\b", blob):
        return True
    return False


def parse_iw_phy_2ghz_channels(iw_phy_text: str) -> list[tuple[int, int]]:
    """(channel, freq_mhz) 2.4 GHz this phy can beacon on (skip disabled)."""
    out: list[tuple[int, int]] = []
    for m in re.finditer(
        r"\*\s+(\d+)\.0 MHz \[(\d+)\]([^\n]*)", iw_phy_text or ""
    ):
        freq, ch, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (2400 <= freq < 2500):
            continue
        if "disabled" in rest.lower():
            continue
        out.append((ch, freq))
    return out


def parse_iw_survey_busy_ratio(survey_text: str) -> dict[int, float]:
    """freq_mhz -> busy/active from `iw survey dump`. Skip empty samples."""
    out: dict[int, float] = {}
    freq = None
    active = busy = None
    for line in (survey_text or "").splitlines():
        m = re.search(r"frequency:\s*(\d+)\s*MHz", line)
        if m:
            if freq is not None and active and active > 0 and busy is not None:
                out[freq] = busy / active
            freq = int(m.group(1))
            active = busy = None
            continue
        m = re.search(r"channel active time:\s*(\d+)", line)
        if m:
            active = int(m.group(1))
            continue
        m = re.search(r"channel busy time:\s*(\d+)", line)
        if m:
            busy = int(m.group(1))
            continue
    if freq is not None and active and active > 0 and busy is not None:
        out[freq] = busy / active
    return out


def first_quiet_2ghz_oper(
    local_2ghz: list[tuple[int, int]],
    occupied_mhz: tuple[int, int] | None = None,
    busy_ratio: Mapping[int, float] | None = None,
    prefer_freq: int | None = None,
) -> tuple[str, str, int] | None:
    """2.4 GHz GO oper preference (never p2p_connect freq=).

    Prefer the peer listen frequency when it is a usable 2.4 channel so
    the sink does not hop off the Neg channel. Else quietest survey.
    No survey: social 6, then 11, then 1 (not first-listed = 1).
    """
    if not local_2ghz:
        return None

    def ok(freq: int) -> bool:
        if occupied_mhz:
            lo, hi = occupied_mhz
            if lo <= freq <= hi:
                return False
        return True

    cands = [(ch, freq) for ch, freq in local_2ghz if ok(freq)]
    if not cands:
        cands = list(local_2ghz)
    if prefer_freq and 2400 <= prefer_freq < 2500:
        for ch, freq in cands:
            if freq == prefer_freq:
                return "81", str(ch), freq
    ratios = busy_ratio or {}
    surveyed = [(ch, freq) for ch, freq in cands if freq in ratios]
    if surveyed:
        ch, freq = min(surveyed, key=lambda cf: ratios[cf[1]])
        return "81", str(ch), freq
    for want in (6, 11, 1):
        for ch, freq in cands:
            if ch == want:
                return "81", str(ch), freq
    ch, freq = cands[0]
    return "81", str(ch), freq


def p2p_no_go_freq_ranges(
    all_freqs: list[int], keep_freqs: set[int]
) -> str:
    """wpa SET p2p_no_go_freq value: ranges covering all_freqs except keep.

    Consecutive phy channels are merged. A kept freq is never inside a range.
    """
    ordered = sorted({int(f) for f in all_freqs if f > 0})
    keep = {int(f) for f in keep_freqs}
    exclude = [f for f in ordered if f not in keep]
    if not exclude:
        return ""
    ranges: list[tuple[int, int]] = []
    start = prev = exclude[0]
    for f in exclude[1:]:
        # Do not swallow a kept channel, and do not merge 2.4 into 5 GHz.
        if any(prev < k < f for k in keep) or (f - prev > 100):
            ranges.append((start, prev))
            start = prev = f
            continue
        prev = f
    ranges.append((start, prev))
    parts = [f"{a}-{b}" if a != b else str(a) for a, b in ranges]
    return ",".join(parts)


def parse_iw_phy_5ghz_channels(iw_phy_text: str) -> list[tuple[int, int]]:
    """(channel, freq_mhz) this phy can beacon on (skip disabled / no-IR)."""
    out: list[tuple[int, int]] = []
    for m in re.finditer(
        r"\*\s+(\d+)\.0 MHz \[(\d+)\]([^\n]*)", iw_phy_text or ""
    ):
        freq, ch, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        if freq < 5000:
            continue
        low = rest.lower()
        if "disabled" in low or "no ir" in low:
            continue
        out.append((ch, freq))
    return out


def parse_sta_occupied_mhz(iw_dev_text: str) -> tuple[int, int] | None:
    """Inclusive MHz range occupied by a STA BSS, from `iw dev` info."""
    m = re.search(
        r"channel\s+\d+\s+\((\d+) MHz\).*?width:\s*(\d+) MHz",
        iw_dev_text or "",
        re.DOTALL,
    )
    if not m:
        return None
    freq, width = int(m.group(1)), int(m.group(2))
    c = re.search(r"center1:\s*(\d+) MHz", iw_dev_text or "")
    center = int(c.group(1)) if c else freq
    half = max(10, width // 2)
    return (center - half, center + half)


def oper_class_for_5ghz_channel(channel: int) -> str:
    """20 MHz 5 GHz global op class (not VHT80 class 128)."""
    if 36 <= channel <= 48:
        return "115"
    if 52 <= channel <= 64:
        return "118"
    if 100 <= channel <= 144:
        return "121"
    if 149 <= channel <= 165:
        return "124"
    return "115"


def first_quiet_5ghz_oper(
    local_5ghz: list[tuple[int, int]],
    occupied_mhz: tuple[int, int] | None = None,
) -> tuple[str, str, int] | None:
    """First 5 GHz channel this phy can use that is outside occupied_mhz.

    Preference only (p2p_oper_*), never p2p_connect freq=. wpa still
    intersects with the peer during GO Neg.
    """
    if not local_5ghz:
        return None
    for ch, freq in local_5ghz:
        if occupied_mhz:
            lo, hi = occupied_mhz
            if lo <= freq <= hi:
                continue
        return oper_class_for_5ghz_channel(ch), str(ch), freq
    ch, freq = local_5ghz[0]
    return oper_class_for_5ghz_channel(ch), str(ch), freq


def p2p_go_width_flags(caps: Mapping[str, bool]) -> tuple[str, str]:
    """(p2p_go_ht40, p2p_go_vht) as '0'/'1' from local phy caps.

    These are advertisements. wpa still intersects with the peer during GO
    Neg (it will drop class 128 if the sink only lists class 124, etc.).
    Do not pin p2p_oper_channel / p2p_connect freq=.
    """
    ht40 = "1" if caps.get("has_ht40") else "0"
    vht = "1" if caps.get("has_vht80") else "0"
    return ht40, vht


def miracast_go_width_flags() -> tuple[str, str]:
    """Miracast GO HT40/VHT: always 20 MHz.

    USB 1x1 at typical TV RSSI (~-85 dBm) on 80 MHz falls to MCS 0 (~7 Mbps)
    with high retries. 20 MHz concentrates power; 5 GHz channel pick is
    separate (peer 5 GHz + first quiet local channel).
    """
    return "0", "0"


def peer_wps_not_ready(peer_info: Mapping[str, str]) -> bool:
    wps = (peer_info.get("wps_method") or "").lower()
    return wps in ("not-ready", "not_ready", "")


def wait_peer_wps_ready(
    wpa_cli: WpaCli,
    peer: str,
    *,
    timeout_s: float = 12.0,
    poll_s: float = 1.0,
) -> dict[str, str]:
    """Poll p2p_peer until WPS is usable, or return the last info on timeout.

    Many sinks ACK Action frames while listening but only send a GO Neg
    Response after the user puts them in Miracast/WPS receive mode.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    info: dict[str, str] = {}
    while True:
        raw = wpa_cli("p2p_peer", peer)
        stdout = getattr(raw, "stdout", None) or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        info = parse_p2p_peer_info(str(stdout))
        if info and not peer_wps_not_ready(info):
            return info
        if time.monotonic() >= deadline:
            return info
        time.sleep(max(0.05, poll_s))


def sync_before_go_neg(
    wpa_cli: WpaCli,
    peer: str,
    listen_freq: int,
    *,
    listen_seconds: int = 8,
    prov_disc_settle_s: float = 2.5,
) -> None:
    """Park on peer listen_freq, then Provision Discovery before GO Neg.

    Order matters for radios with fragile off-channel Action TX: start
    p2p_listen first so the radio is already on listen_freq, then send PD.
    Do not call p2p_listen after PD — that can free the pending Action work.
    """
    wpa_cli("p2p_stop_find")
    wpa_cli("p2p_cancel")
    time.sleep(0.3)

    print(
        f"[FluxCast WFD] P2P sync: listen then prov_disc on {listen_freq} MHz "
        "before GO Neg"
    )
    wpa_cli("p2p_listen", str(listen_seconds))
    time.sleep(0.4)

    pd = wpa_cli("p2p_prov_disc", peer, "pbc")
    pd_out = (
        getattr(pd, "stdout", None) or getattr(pd, "stderr", None) or ""
    )
    if isinstance(pd_out, bytes):
        pd_out = pd_out.decode(errors="replace")
    pd_out = str(pd_out).strip()
    rc = getattr(pd, "returncode", 0)
    if rc != 0 or "FAIL" in pd_out.upper():
        print(f"[FluxCast WFD] p2p_prov_disc soft-fail ({pd_out}); continuing")
    else:
        time.sleep(prov_disc_settle_s)


def neg_outcome_from_log(new_text: str) -> str:
    """Return 'success', 'failure', or 'pending' from wpa log delta text.

    TX_WAIT_EXPIRE is *not* formation failure. wpa retries GO Neg Request
    after the off-channel wait (~500 ms) expires; leftover Provision
    Discovery wait-expire can also land after the log mark. Callers should
    wait for GROUP-STARTED / GO-NEG-SUCCESS, real GO-NEG-FAILURE, or their
    own deadline.
    """
    if "P2P-GROUP-STARTED" in new_text or "P2P-GO-NEG-SUCCESS" in new_text:
        return "success"
    if "P2P-GO-NEG-FAILURE" in new_text or "P2P-GROUP-FORMATION-FAILURE" in new_text:
        return "failure"
    return "pending"


def describe_neg_log(new_text: str) -> str:
    """Human-readable reason when GO Neg did not finish within the deadline."""
    acked = "GO Negotiation Request TX callback: success=1" in new_text
    nacked = "GO Negotiation Request TX callback: success=0" in new_text
    got_resp = "GO Negotiation Response" in new_text
    sent = "Sending GO Negotiation Request" in new_text
    if acked and not got_resp:
        return (
            "GO Neg Request was ACKed but no Neg Response arrived "
            "(sink not in receive/WPS, or off-channel wait dropped before RX)"
        )
    if nacked and not acked:
        return "GO Neg Request TX was not ACKed (off-channel Action TX failed)"
    if sent and not got_resp:
        return "GO Neg Request sent, no Response"
    return "no GO-NEG-SUCCESS / GROUP-STARTED"


def is_pending_go_shell(iw_info: str) -> bool:
    """True for a P2P-GO netdev without channel/SSID (pre-Neg scaffolding)."""
    return (
        "type P2P-GO" in iw_info
        and "channel" not in iw_info
        and "ssid" not in iw_info
    )
