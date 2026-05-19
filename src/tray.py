import os
import sys
import subprocess
import threading
import webbrowser

os.environ.setdefault("PYSTRAY_BACKEND", "appindicator")

from PIL import Image
import pystray

_BASE = os.path.dirname(os.path.abspath(__file__))
_ICON_PATH = os.path.join(_BASE, "assets", "flcast_logo_512x512.png")
_MAIN = os.path.join(_BASE, "main.py")
_PY = sys.executable
_LOG_PATH = "/tmp/fluxcast-cast.log"


def _is_wayland() -> bool:
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland" \
        or bool(os.environ.get("WAYLAND_DISPLAY"))


def _is_hyprland() -> bool:
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    return "hyprland" in desktop or bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def _wfd_uses_portal() -> bool:
    """Mirror wfd.start_experimental_backend()'s auto-portal logic."""
    return _is_wayland() and not _is_hyprland()

_lock = threading.Lock()
_proc = None
_cast_target = ""
_icon = None
_refresh_pending = False

_wfd_peers: list = []
_dlna_devices: list = []
_cast_devices: list = []
_monitors: list = []
_selected_monitor_idx = 0

_scanning_wfd = False
_scanning_dlna = False
_scanning_cast = False
_scanning_monitors = False
_about_thread = None


def _log(msg: str) -> None:
    print(f"[Tray] {msg}", flush=True)


def _notify(title: str, body: str) -> None:
    subprocess.Popen(
        ["notify-send", "-a", "FluxCast", title, body],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _refresh() -> None:
    global _refresh_pending
    if _icon is None:
        return
    with _lock:
        if _refresh_pending:
            return
        _refresh_pending = True

    def _do_update():
        global _refresh_pending
        with _lock:
            _refresh_pending = False
        try:
            items = _build_menu()
            if hasattr(_icon, '_appindicator') and hasattr(_icon, '_create_menu'):
                gtk_menu = _icon._create_menu(pystray.Menu(*items))
                if gtk_menu is not None:
                    _icon._menu_handle = gtk_menu
                    _icon._appindicator.set_menu(gtk_menu)
                else:
                    _icon.update_menu()
            else:
                _icon.update_menu()
        except Exception as e:
            _log(f"menu rebuild error: {e}")
            try:
                _icon.update_menu()
            except Exception:
                pass
        return False

    try:
        from gi.repository import GLib
        GLib.idle_add(_do_update)
    except Exception as e:
        _log(f"GLib schedule failed: {e}")
        with _lock:
            _refresh_pending = False
        try:
            _icon.update_menu()
        except Exception:
            pass


def _load_image() -> Image.Image:
    try:
        with Image.open(_ICON_PATH) as img:
            img.load()
            out = img.convert("RGBA")
        _log(f"icon loaded: {_ICON_PATH} ({out.size})")
        return out
    except Exception as e:
        _log(f"icon load FAILED ({e}); using red 64x64 fallback")
        return Image.new("RGBA", (64, 64), (255, 0, 0, 255))


# ── subprocess management ─────────────────────────────────────────────────────

def _watch(proc: "subprocess.Popen", target: str) -> None:
    proc.wait()
    global _proc, _cast_target
    with _lock:
        if _proc is proc:
            _proc = None
            _cast_target = ""
    rc = proc.returncode
    if rc == 0:
        _notify("FluxCast stopped", f"Cast to {target} ended cleanly.")
    else:
        _notify("FluxCast stopped", f"Cast to {target} exited (code {rc}).")
    _refresh()


def _launch(cmd: list, target: str) -> None:
    global _proc, _cast_target
    log_fp = open(_LOG_PATH, "ab", buffering=0)
    log_fp.write(f"\n=== {target} :: {' '.join(cmd)} ===\n".encode())
    proc = subprocess.Popen(cmd, stdout=log_fp, stderr=subprocess.STDOUT)
    log_fp.close()
    with _lock:
        _proc = proc
        _cast_target = target
    _notify("FluxCast", f"Starting cast to {target}… (log: {_LOG_PATH})")
    _log(f"launched: {' '.join(cmd)}")
    _refresh()
    threading.Thread(target=_watch, args=(proc, target), daemon=True).start()


def _stop() -> None:
    with _lock:
        proc = _proc
    if proc and proc.poll() is None:
        proc.terminate()


def _on_exit(icon, item) -> None:
    _stop()
    icon.stop()


# ── discovery ─────────────────────────────────────────────────────────────────

def _scan_wfd() -> None:
    global _wfd_peers, _scanning_wfd
    with _lock:
        _scanning_wfd = True
    _refresh()
    try:
        from wfd import active_scan
        peers = active_scan(timeout=8)
    except Exception as e:
        _log(f"wfd scan error: {e}")
        peers = []
    with _lock:
        _wfd_peers = peers
        _scanning_wfd = False
    _log(f"wfd scan done: {len(peers)} peers")
    _refresh()


def _scan_dlna() -> None:
    global _dlna_devices, _scanning_dlna
    with _lock:
        _scanning_dlna = True
    _refresh()
    try:
        from dlna import discover_devices
        devices = discover_devices(timeout=5)
    except Exception as e:
        _log(f"dlna scan error: {e}")
        devices = []
    with _lock:
        _dlna_devices = devices
        _scanning_dlna = False
    _log(f"dlna scan done: {len(devices)} devices")
    _refresh()


def _scan_cast() -> None:
    global _cast_devices, _scanning_cast
    with _lock:
        _scanning_cast = True
    _refresh()
    try:
        from cast import discover_devices
        devices = discover_devices(timeout=5)
    except Exception as e:
        _log(f"cast scan error: {e}")
        devices = []
    with _lock:
        _cast_devices = devices
        _scanning_cast = False
    _log(f"cast scan done: {len(devices)} devices")
    _refresh()


def _load_monitors() -> None:
    global _monitors, _scanning_monitors
    with _lock:
        _scanning_monitors = True
    _refresh()
    try:
        from capture import gather_monitors
        monitors = gather_monitors()
    except Exception as e:
        _log(f"monitor scan error: {e}")
        monitors = []
    with _lock:
        _monitors = monitors
        _scanning_monitors = False
    _log(f"monitor scan done: {len(monitors)} monitors")
    _refresh()


def _rescan_all() -> None:
    _log("starting all 4 discovery threads")
    for fn in (_scan_wfd, _scan_dlna, _scan_cast, _load_monitors):
        threading.Thread(target=fn, daemon=True).start()


# ── cast actions ──────────────────────────────────────────────────────────────

def _start_wfd(peer, monitor) -> None:
    cmd = [_PY, _MAIN, "--protocol", "wfd", "--wfd-peer", peer.address]
    # On non-Hyprland Wayland the WFD backend uses xdg-portal and shows its
    if monitor is not None and not _wfd_uses_portal():
        cmd += ["--wfd-monitor", monitor.name]
    _launch(cmd, peer.name or peer.address)


def _start_dlna(device, monitor) -> None:
    cmd = [_PY, _MAIN, "--protocol", "dlna", "--device-name", device.friendly_name]
    if monitor is not None:
        cmd += ["--monitor", monitor.name]
    _launch(cmd, device.friendly_name)


def _start_cast(device, monitor) -> None:
    name = device.cast_info.friendly_name
    cmd = [_PY, _MAIN, "--protocol", "cast", "--device-name", name]
    if monitor is not None:
        cmd += ["--monitor", monitor.name]
    _launch(cmd, name)


def _select_monitor(idx: int) -> None:
    global _selected_monitor_idx
    with _lock:
        _selected_monitor_idx = idx
    _refresh()


# ── about window ─────────────────────────────────────────────────────────────

def _show_about() -> None:
    global _about_thread
    if _about_thread is not None and _about_thread.is_alive():
        return

    def _run():
        import tkinter as tk
        from PIL import ImageTk

        BG = "#0a120d"
        FG = "#e5e7eb"
        ACCENT = "#30D987"
        LINK = "#60a5fa"
        SEP = "#1e3a26"
        BTN_BG = "#1a2e1f"

        root = tk.Tk()
        root.title("About FluxCast")
        root.resizable(False, False)
        root.configure(bg=BG)

        frame = tk.Frame(root, bg=BG, padx=28, pady=20)
        frame.pack()

        try:
            img = Image.open(_ICON_PATH).resize((72, 72), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            tk.Label(frame, image=photo, bg=BG).pack(pady=(0, 8))
            root._photo = photo
        except Exception:
            pass

        tk.Label(frame, text="FluxCast", font=("sans-serif", 16, "bold"),
                 bg=BG, fg=FG).pack()
        tk.Label(frame, text="Desktop → Smart TV streaming for Linux",
                 font=("sans-serif", 10), bg=BG, fg=ACCENT).pack(pady=(2, 14))

        tk.Frame(frame, bg=SEP, height=1).pack(fill="x", pady=(0, 12))

        story = (
            "I wanted to watch movies by streaming my Linux desktop to a TV — "
            "nothing worked. gnome-network-displays gave me one frame and then "
            "froze. miraclecast hasn't been meaningfully updated in years.\n\n"
            "So I built FluxCast from scratch: Wi-Fi Direct via NetworkManager, "
            "full RTSP handshake, RTP media stream. ~1 second latency, "
            "video and audio. It actually works."
        )
        tk.Label(frame, text=story, font=("sans-serif", 10), wraplength=360,
                 justify="left", bg=BG, fg=FG).pack(pady=(0, 14))

        tk.Frame(frame, bg=SEP, height=1).pack(fill="x", pady=(0, 10))

        for text, url in [
            ("fluxcast.secweb.cloud", "https://fluxcast.secweb.cloud/"),
            ("github.com/IlyaP358/fluxcast", "https://github.com/IlyaP358/fluxcast"),
        ]:
            lbl = tk.Label(frame, text=text, fg=LINK, cursor="hand2",
                           font=("sans-serif", 10, "underline"), bg=BG)
            lbl.pack(anchor="w")
            lbl.bind("<Button-1>", lambda _, u=url: webbrowser.open(u))

        tk.Frame(frame, bg=SEP, height=1).pack(fill="x", pady=(12, 10))

        FG_DIM = "#6b7280"
        for text, url in [
            ("illia.pukalov@teleinformatika.eu", "mailto:illia.pukalov@teleinformatika.eu"),
            ("Join our Discord", "https://discord.gg/Qa2UTZPpPh"),
            ("View Contributors", "https://fluxcast.secweb.cloud/contributors.html"),
        ]:
            lbl = tk.Label(frame, text=text, fg=FG_DIM, cursor="hand2",
                           font=("sans-serif", 8, "underline"), bg=BG)
            lbl.pack()
            lbl.bind("<Button-1>", lambda _, u=url: webbrowser.open(u))

        tk.Label(frame, text="Author: IlyaP358  |  Code licensed under GPL-3.0",
                 font=("sans-serif", 7), bg=BG, fg=FG_DIM).pack(pady=(8, 0))

        tk.Button(frame, text="Close", command=root.destroy, width=10,
                  bg=BTN_BG, fg=FG, activebackground="#253d2a",
                  activeforeground=FG, relief="flat",
                  cursor="hand2").pack(pady=(12, 0))
        root.mainloop()

    _about_thread = threading.Thread(target=_run, daemon=True)
    _about_thread.start()


# ── menu ──────────────────────────────────────────────────────────────────────

def _act(fn, *args):
    """2-arg pystray action wrapper. pystray rejects lambdas with >2 positional args."""
    def handler(icon, item):
        fn(*args)
    return handler


def _build_menu():
    with _lock:
        proc = _proc
        target = _cast_target
        peers = list(_wfd_peers)
        dlna = list(_dlna_devices)
        cast_devs = list(_cast_devices)
        monitors = list(_monitors)
        sel_mon = _selected_monitor_idx
        sc_wfd = _scanning_wfd
        sc_dlna = _scanning_dlna
        sc_cast = _scanning_cast
        sc_mon = _scanning_monitors

    items = []

    casting = proc is not None and proc.poll() is None
    status = f"● Casting: {target}" if casting else "● Idle"
    items.append(pystray.MenuItem(status, None, enabled=False))
    items.append(pystray.Menu.SEPARATOR)

    if casting:
        items.append(pystray.MenuItem("Stop Casting", lambda *_: _stop()))
    else:
        if monitors:
            sel_mon = min(sel_mon, len(monitors) - 1)
            mon_items = [
                pystray.MenuItem(
                    f"{m.name}  ({m.width}×{m.height})",
                    _act(_select_monitor, i),
                    checked=lambda item, i=i, sel=sel_mon: sel == i,
                )
                for i, m in enumerate(monitors)
            ]
            items.append(pystray.MenuItem("Monitor", pystray.Menu(*mon_items)))
        elif sc_mon:
            items.append(pystray.MenuItem("Monitor: scanning…", None, enabled=False))

        sel = monitors[sel_mon] if monitors else None  # sel_mon already clamped above
        wfd_via_portal = _wfd_uses_portal()

        if peers:
            if wfd_via_portal or sel is not None:
                peer_items = [
                    pystray.MenuItem(
                        p.name or p.address,
                        _act(_start_wfd, p, sel),
                    )
                    for p in peers
                ]
            else:
                peer_items = [pystray.MenuItem("Select a monitor first", None, enabled=False)]
            if sc_wfd:
                peer_items.append(pystray.MenuItem("↻ Refreshing…", None, enabled=False))
            wfd_sub = pystray.Menu(*peer_items)
        elif sc_wfd:
            wfd_sub = pystray.Menu(pystray.MenuItem("Scanning…", None, enabled=False))
        else:
            wfd_sub = pystray.Menu(pystray.MenuItem("No WFD devices found", None, enabled=False))
        wfd_label = "Cast via Miracast (WFD, portal dialog)" if wfd_via_portal else "Cast via Miracast (WFD)"
        items.append(pystray.MenuItem(wfd_label, wfd_sub))

        if dlna:
            if sel is not None:
                dlna_items = [
                    pystray.MenuItem(
                        d.friendly_name,
                        _act(_start_dlna, d, sel),
                    )
                    for d in dlna
                ]
            else:
                dlna_items = [pystray.MenuItem("Select a monitor first", None, enabled=False)]
            if sc_dlna:
                dlna_items.append(pystray.MenuItem("↻ Refreshing…", None, enabled=False))
            dlna_sub = pystray.Menu(*dlna_items)
        elif sc_dlna:
            dlna_sub = pystray.Menu(pystray.MenuItem("Scanning…", None, enabled=False))
        else:
            dlna_sub = pystray.Menu(pystray.MenuItem("No DLNA devices found", None, enabled=False))
        items.append(pystray.MenuItem("Cast via DLNA/UPnP", dlna_sub))

        if cast_devs:
            if sel is not None:
                cast_items = [
                    pystray.MenuItem(
                        d.cast_info.friendly_name,
                        _act(_start_cast, d, sel),
                    )
                    for d in cast_devs
                ]
            else:
                cast_items = [pystray.MenuItem("Select a monitor first", None, enabled=False)]
            if sc_cast:
                cast_items.append(pystray.MenuItem("↻ Refreshing…", None, enabled=False))
            cast_sub = pystray.Menu(*cast_items)
        elif sc_cast:
            cast_sub = pystray.Menu(pystray.MenuItem("Scanning…", None, enabled=False))
        else:
            cast_sub = pystray.Menu(pystray.MenuItem("No Chromecast devices found", None, enabled=False))
        items.append(pystray.MenuItem("Cast via Chromecast", cast_sub))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Rescan Devices", lambda *_: _rescan_all()))

    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("About FluxCast", lambda *_: _show_about()))
    items.append(pystray.MenuItem("Exit", _on_exit))

    return items


def run_tray() -> None:
    global _icon
    _log("run_tray() starting")
    img = _load_image()
    icon = pystray.Icon(
        "FluxCast",
        img,
        title="FluxCast",
        menu=pystray.Menu(lambda: _build_menu()),
    )
    _icon = icon
    _log(f"Icon created, class={type(icon).__module__}.{type(icon).__name__}")

    def kickoff():
        _log("kickoff: GTK loop is alive, starting scans")
        _rescan_all()
        return False  # don't repeat

    try:
        from gi.repository import GLib
        GLib.idle_add(kickoff)
        _log("kickoff queued via GLib.idle_add")
    except Exception as e:
        _log(f"GLib.idle_add failed ({e}); falling back to thread")
        threading.Thread(target=_rescan_all, daemon=True).start()

    _log("calling icon.run() — default setup will set visible=True")
    icon.run()
    _log("icon.run() returned — tray stopped")
