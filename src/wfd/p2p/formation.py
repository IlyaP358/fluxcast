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


def listen_freq_for_wps(
    peer_info: Mapping[str, str],
    cached_listen_mhz: int | None,
    default: int = 2437,
) -> int:
    """Listen channel for GO Neg / WPS.

    A 2.4 GHz social find can snapshot a probe while the sink still
    listens on 5 GHz. Prefer the scan-cache 5 GHz listen so WPS stays
    on the Neg channel (Wi-Fi Direct).
    """
    live = peer_listen_freq(peer_info, default=default)
    if cached_listen_mhz and cached_listen_mhz >= 5000 and live < 5000:
        return int(cached_listen_mhz)
    return live


def freq_to_p2p_class_channel(freq: int) -> tuple[str, str]:
    """Map MHz → (global oper class, channel) for p2p_listen_* / p2p_pref_chan.

    Wi-Fi P2P uses operating classes (81 = 2.4 GHz 20 MHz, 115/118/121/124 =
    5 GHz 20 MHz). A 5 GHz listen freq must not collapse to class 81.
    """
    if 2400 <= freq < 2500:
        ch = {2412: 1, 2437: 6, 2462: 11}.get(freq)
        if ch is None:
            ch = (freq - 2407) // 5
        return "81", str(ch)
    if 5000 <= freq <= 5925:
        ch = (freq - 5000) // 5
        return oper_class_for_5ghz_channel(ch), str(ch)
    return "81", "6"


def freq_to_social_channel(freq: int) -> tuple[str, str]:
    """Alias: listen/oper class+channel for any band (not 2.4-only)."""
    return freq_to_p2p_class_channel(freq)


def peer_advertised_freqs(peer_info: Mapping[str, str]) -> set[int]:
    """Frequencies the peer listed (listen/oper + P2P channel_list)."""
    out: set[int] = set()
    for key in ("listen_freq", "oper_freq", "freq"):
        try:
            freq = int(peer_info.get(key) or 0)
        except ValueError:
            continue
        if 2400 <= freq <= 5925:
            out.add(freq)
    blob = " ".join(
        str(peer_info.get(k) or "")
        for k in ("channels", "channel_list", "freq_list")
    )
    for m in re.finditer(
        r"\b(81|115|118|121|124|125):([0-9,]+)", blob
    ):
        cls, chans = m.group(1), m.group(2)
        for token in chans.split(","):
            if not token.strip().isdigit():
                continue
            ch = int(token.strip())
            if cls == "81":
                out.add(2407 + 5 * ch)
            else:
                out.add(5000 + 5 * ch)
    return out


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


def parse_iw_phy_radio(iw_phy_text: str) -> dict[str, object]:
    """Phy capabilities used for P2P GO Neg (Wi-Fi Direct Channel List / width).

    Bus (USB vs PCI) is not an input. NSS, antenna mask, and per-band TX
    power come from ``iw phy``. Defaults are conservative (1 stream).
    """
    t = iw_phy_text or ""
    caps = parse_iw_phy_caps(t)
    nss = 1
    for m in re.finditer(
        r"HT TX/RX MCS rate indexes supported:\s*0-(\d+)", t
    ):
        hi = int(m.group(1))
        if hi >= 23:
            nss = max(nss, 3)
        elif hi >= 15:
            nss = max(nss, 2)
        else:
            nss = max(nss, 1)
    for m in re.finditer(r"^(\d+) streams:", t, re.M):
        nss = max(nss, int(m.group(1)))
    antennas_tx = 1
    m = re.search(r"Available Antennas:\s*TX\s*(0x[0-9a-fA-F]+)", t)
    if m:
        bits = bin(int(m.group(1), 16)).count("1")
        if bits:
            antennas_tx = bits

    def _max_tx(lo: int, hi: int) -> float | None:
        best: float | None = None
        for tm in re.finditer(
            r"\*\s+(\d+)\.0 MHz \[(\d+)\]\s*\(([\d.]+)\s*dBm\)", t
        ):
            freq = int(tm.group(1))
            if lo <= freq < hi:
                dbm = float(tm.group(3))
                best = dbm if best is None else max(best, dbm)
        return best

    return {
        **caps,
        "nss": nss,
        "antennas_tx": antennas_tx,
        "tx_dbm_24": _max_tx(2400, 2500),
        "tx_dbm_5": _max_tx(5000, 5925),
        "has_he": "HE Capabilities" in t or "HE Iftypes" in t or "HE PHY" in t,
    }


def radio_can_stream_5ghz(radio: Mapping[str, object]) -> bool:
    """True if this phy can reasonably carry WFD on 5 GHz (not a bus check)."""
    if not radio.get("has_5ghz"):
        return False
    nss = int(radio.get("nss") or 1)
    ants = int(radio.get("antennas_tx") or 1)
    if nss >= 2 or ants >= 2:
        return True
    tx5 = radio.get("tx_dbm_5")
    tx24 = radio.get("tx_dbm_24")
    try:
        tx5f = float(tx5) if tx5 is not None else None
        tx24f = float(tx24) if tx24 is not None else None
    except (TypeError, ValueError):
        tx5f = tx24f = None
    # 5 GHz has extra path loss. 1ss needs a real EIRP advantage to stay.
    if tx5f is not None and tx5f >= 20:
        return True
    if tx5f is not None and tx24f is not None and tx5f >= tx24f + 6:
        return True
    return False


def radio_meets_wfd_24ghz(radio: Mapping[str, object]) -> bool:
    """2.4 GHz 20 MHz 1ss is enough at short range (hotyeah ~−46 dBm)."""
    if not radio.get("has_2ghz"):
        return False
    tx = radio.get("tx_dbm_24")
    if tx is None:
        return True
    try:
        return float(tx) >= 10.0
    except (TypeError, ValueError):
        return True


def wfd_radio_reject_reason(
    radio: Mapping[str, object],
    oper_mhz: int,
    *,
    allow_weak: bool = False,
    peer_has_24: bool = True,
) -> str | None:
    """Why this phy cannot carry a *reliable* WFD payload, or None.

    WFD does not define a TX-power minimum. Refuse 5 GHz *payload* on a
    1-stream / &lt;20 dBm phy (−80 dBm MCS 2 on LG). Do **not** refuse
    the session if 2.4 GHz payload is viable (same phy, hotyeah ~−46 dBm;
    LG still forms on 5 GHz listen for WPS then CSA). ``allow_weak`` is
    FLUXCAST_WFD_ALLOW_WEAK_RADIO (5 GHz payload on a weak phy).
    """
    if allow_weak or oper_mhz <= 0:
        return None
    nss = int(radio.get("nss") or 1)
    ants = int(radio.get("antennas_tx") or 1)
    tx5 = radio.get("tx_dbm_5")
    tx24 = radio.get("tx_dbm_24")
    if oper_mhz >= 5000 and not radio_can_stream_5ghz(radio):
        if peer_has_24 and radio_meets_wfd_24ghz(radio):
            return None
        return (
            f"phy below 5 GHz WFD baseline (nss={nss} antennas={ants} "
            f"tx_5ghz={tx5} dBm; need 2 streams or ≥20 dBm) and no "
            "usable 2.4 GHz payload path. "
            "FLUXCAST_WFD_ALLOW_WEAK_RADIO=1 to override"
        )
    if 2400 <= oper_mhz < 2500 and not radio_meets_wfd_24ghz(radio):
        return (
            f"phy below 2.4 GHz WFD baseline (nss={nss} antennas={ants} "
            f"tx_2ghz={tx24} dBm; need ≥10 dBm). "
            "FLUXCAST_WFD_ALLOW_WEAK_RADIO=1 to override"
        )
    return None


def go_width_flags_for_radio(radio: Mapping[str, object]) -> tuple[str, str]:
    """``(p2p_go_ht40, p2p_go_vht)`` from this phy.

    WFD requires 802.11n (HT20). HT40/VHT are optional; wpa intersects with
    the peer Channel List during GO Neg. A 1-stream radio stays 20 MHz.
    """
    nss = int(radio.get("nss") or 1)
    if nss <= 1:
        return "0", "0"
    return p2p_go_width_flags(radio)


def go_intents_for_radio(
    go_intent: int, radio: Mapping[str, object]
) -> list[int]:
    """GO intent list. WFD: either source or sink may be GO (single sink).

    1-stream / 1-antenna radios often drop client Confirm TX (SoftMAC
    remain-on-channel), so they stay GO (15). Stronger radios honor the
    requested intent and can fall back.
    """
    nss = int(radio.get("nss") or 1)
    ants = int(radio.get("antennas_tx") or 1)
    if nss <= 1 and ants <= 1:
        return [15]
    if go_intent <= 0:
        return [0, 15]
    if go_intent >= 15:
        return [15, 0]
    return [go_intent]


def peer_advertises_24ghz(peer_info: Mapping[str, str]) -> bool:
    """True unless the peer Channel List is present and 5 GHz-only.

    listen_freq=5745 with no channel_list is not proof the sink lacks
    2.4 GHz social channels (1/6/11). That snapshot used to skip CSA.
    """
    blob = " ".join(
        str(peer_info.get(k) or "")
        for k in ("channels", "channel_list", "freq_list")
    )
    if re.search(r"\b81:", blob):
        return True
    for key in ("listen_freq", "oper_freq", "freq"):
        try:
            freq = int(peer_info.get(key) or 0)
        except ValueError:
            continue
        if 2400 <= freq < 2500:
            return True
    if re.search(r"\b(115|118|121|124|125):", blob):
        return False
    return True


# Wi-Fi Direct formation vs 802.11 payload CSA vs WFD L3 (DHCP/RTSP).
FORMATION_STA_WAIT_S = 15.0
FORMATION_L3_TRY_S = 12.0  # DHCP on formation channel before any CSA
POST_CSA_STA_WAIT_S = 32.0  # first 2.4 AP-STA-CONNECTED after CSA
POST_CSA_CHURN_S = 32.0  # LG second 4-way ~28s after first 2.4 join
POST_CSA_SETTLE_S = 8.0  # quiet time after the last 4-way before DHCP
PAYLOAD_CSA_24_MHZ = 2437
PAYLOAD_STAY = "stay"
PAYLOAD_CSA_24 = "csa_2437"


def go_sta_is_present(log_delta: str, station_dump: str) -> bool:
    """True if a P2P client is associated with this GO.

    Wi-Fi Direct WPS + 4-way run on the negotiated operating channel
    (typically the peer listen channel). 802.11 CSA is for an *already
    associated* STA. CSA before AP-STA-CONNECTED is why LG formation
    failed after 5 GHz WPS (GROUP-REMOVED FORMATION_FAILED, no STA).
    """
    dump = station_dump or ""
    if re.search(r"(?m)^Station ", dump):
        return True
    delta = log_delta or ""
    conn = delta.rfind("AP-STA-CONNECTED")
    if conn < 0:
        return False
    disc = delta.rfind("AP-STA-DISCONNECTED")
    return conn > disc


def ap_sta_connect_count(log_delta: str) -> int:
    """Unprefixed CTRL-EVENT lines only (hostapd also logs ``p2p-0:``)."""
    return len(re.findall(r"(?m)^AP-STA-CONNECTED ", log_delta or ""))


def go_sta_connected_in_delta(log_delta: str) -> bool:
    """True if this log slice has a CONNECT after any DISCONNECT.

    Post-CSA L3 must not use ``iw station dump``: the 5 GHz association
    stays listed (zombie, low inactive time for ~1s) and would start the
    DHCP clock before the sink rejoins on 2.4.
    """
    delta = log_delta or ""
    conn = delta.rfind("AP-STA-CONNECTED")
    if conn < 0:
        return False
    disc = delta.rfind("AP-STA-DISCONNECTED")
    return conn > disc


def csa_sta_followed(
    *,
    go_mhz: int,
    target_mhz: int,
    disconnected: bool,
    inactive_ms: int | None,
    sta_present: bool,
    max_inactive_ms: int = 2000,
) -> bool:
    """True only if the same association is live on the CSA target channel.

    A single sample right after chan_switch is not enough (LG looked
    followed at 2.5s then went MCS 0 / 10s inactive and TCP timed out).
    """
    if disconnected or not sta_present or go_mhz != target_mhz:
        return False
    if inactive_ms is None:
        return False
    return inactive_ms < max_inactive_ms


def csa_dhcp_still_waiting(
    *,
    connected: bool,
    seconds_since_last_connect: float | None,
    seconds_since_first_connect: float,
    extra_four_ways: int,
    churn_s: float = POST_CSA_CHURN_S,
    settle_s: float = POST_CSA_SETTLE_S,
) -> bool:
    """True if DHCP would still land on an association LG is about to replace.

    After CSA the sink often 4-way once (DHCP/RTSP start), then a full
    re-auth ~28s later which kills TCP, then deauth reason 3. Wait until
    either a second 4-way has settled, or the churn window has passed
    with one stable join (hotyeah / no extra auth).
    """
    if not connected or seconds_since_last_connect is None:
        return True
    if seconds_since_last_connect < settle_s:
        return True
    if extra_four_ways < 1 and seconds_since_first_connect < churn_s:
        return True
    return False


def go_sta_ready_for_l3(
    log_delta: str,
    station_dump: str,
    *,
    max_inactive_ms: int = 5000,
) -> bool:
    """STA is on the *current* BSS and recently active (safe for DHCP/RTSP).

    After CSA the dump can still list the 5 GHz association with a large
    inactive time while the sink scans 2.4. Starting DHCP then burns the
    lease timer (LG ~24s rejoin).
    """
    dump = station_dump or ""
    if not go_sta_is_present(log_delta, dump):
        return False
    m = re.search(r"inactive time:\s*(\d+)\s*ms", dump)
    if m and int(m.group(1)) > max_inactive_ms:
        return False
    return True


def plan_payload_channel(
    *,
    go_mhz: int,
    radio: Mapping[str, object],
    sta_signal_dbm: int | None,
    peer_has_24: bool,
    prefer_5ghz: bool,
    sta_present: bool,
) -> str:
    """Payload channel after formation. CSA without a STA is unrepresentable.

    Formation (WPS/4-way) stays on the negotiated oper channel. CSA is
    802.11 for an associated STA only (hotyeah usually already on 2.4;
    LG 5 GHz listen must associate before any hop).
    """
    if not sta_present:
        return PAYLOAD_STAY
    if should_csa_go_to_2ghz(
        go_mhz=go_mhz,
        radio=radio,
        sta_signal_dbm=sta_signal_dbm,
        peer_has_24=peer_has_24,
        prefer_5ghz=prefer_5ghz,
    ):
        return PAYLOAD_CSA_24
    return PAYLOAD_STAY


def should_csa_go_to_2ghz(
    *,
    go_mhz: int,
    radio: Mapping[str, object],
    sta_signal_dbm: int | None,
    peer_has_24: bool,
    prefer_5ghz: bool,
) -> bool:
    """Move a 5 GHz GO to 2.4 when this phy cannot carry WFD there.

    Formation stays on the peer listen channel for WPS (Wi-Fi Direct).
    CSA is payload only. A 1-stream radio CSAs unless the caller opted
    to stay on 5 GHz. A stronger radio stays unless STA RSSI is poor.
    """
    if go_mhz < 5000:
        return False
    if not peer_has_24:
        return False
    if not radio_can_stream_5ghz(radio):
        # Weak 5 GHz phy: payload must be 2.4 if the peer has it.
        # prefer_5ghz keeps 5 GHz in the *formation* set (WPS), not payload.
        return bool(peer_has_24)
    if prefer_5ghz and (sta_signal_dbm is None or sta_signal_dbm > -82):
        return False
    if sta_signal_dbm is not None and sta_signal_dbm <= -80:
        return True
    return False


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
    prefer_freq: int | None = None,
) -> tuple[str, str, int] | None:
    """First 5 GHz channel this phy can use that is outside occupied_mhz.

    Preference only (p2p_oper_*), never p2p_connect freq=. wpa still
    intersects with the peer during GO Neg.
    """
    if not local_5ghz:
        return None

    def ok(freq: int) -> bool:
        if occupied_mhz:
            lo, hi = occupied_mhz
            if lo <= freq <= hi:
                return False
        return True

    if prefer_freq and prefer_freq >= 5000:
        for ch, freq in local_5ghz:
            if freq == prefer_freq and ok(freq):
                return oper_class_for_5ghz_channel(ch), str(ch), freq
    for ch, freq in local_5ghz:
        if ok(freq):
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


def usb_discovery_steps(cached_listen_mhz: int | None) -> list[dict[str, object]]:
    """Discovery plan: search the known listen channel first.

    A 5 GHz-listen sink was invisible for a full 12 s social find, then
    appeared in ~2 s on ``p2p_find freq=<listen>``. Social find is the
    fallback (TV scanning 1/6/11). Listen-only is last.
    """
    if cached_listen_mhz and cached_listen_mhz >= 5000:
        mhz = int(cached_listen_mhz)
        return [
            {"mode": "find_freq", "seconds": 8, "freq": mhz},
            {"mode": "find_social", "seconds": 8, "freq": None},
            {"mode": "listen", "seconds": 8, "freq": mhz},
            {"mode": "find", "seconds": 8, "freq": None},
        ]
    freq = (
        int(cached_listen_mhz)
        if cached_listen_mhz and 2400 <= cached_listen_mhz < 2500
        else 2437
    )
    return [
        {"mode": "find_social", "seconds": 8, "freq": None},
        {"mode": "listen", "seconds": 8, "freq": freq},
        {"mode": "find", "seconds": 8, "freq": None},
    ]


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
    listen_seconds: int = 2,
    prov_disc_settle_s: float = 0.6,
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
