import os
import sys
import subprocess
import threading
import webbrowser
from version import get_fluxcast_version
from i18n import _l

os.environ.setdefault("PYSTRAY_BACKEND", "appindicator")

from PIL import Image, ImageChops, ImageDraw, ImageFilter
import pystray

from ui.tray_config import load_preferred_monitor, load_profile

# assets/ and main.py sit at the root of src/, one level above this package.
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ICON_PATH = os.path.join(_BASE, "assets", "flcast_logo_512x512.png")
_TRAY_ICON_PATHS = {
    "idle": os.path.join(_BASE, "assets", "tray-idle.png"),
    "casting": os.path.join(_BASE, "assets", "tray-casting.png"),
}
_MAIN = os.path.join(_BASE, "main.py")
_REPO = "https://github.com/IlyaP358/fluxcast"
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
_icon_state = ""
_tray_image_cache: dict = {}
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
        global _refresh_pending, _icon_state
        with _lock:
            _refresh_pending = False
        try:
            state = _cast_state()
            if state != _icon_state:
                _icon.icon = _tray_image(state)
                _icon_state = state
                _log(f"tray icon -> {state}")
        except Exception as e:
            _log(f"icon swap error: {e}")
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


def _load_image(path: str = _ICON_PATH) -> Image.Image:
    try:
        with Image.open(path) as img:
            img.load()
            out = img.convert("RGBA")
        _log(f"icon loaded: {path} ({out.size})")
        return out
    except Exception as e:
        _log(f"icon load FAILED ({e}); using red 64x64 fallback")
        return Image.new("RGBA", (64, 64), (255, 0, 0, 255))


def _tray_image(state: str) -> Image.Image:
    cached = _tray_image_cache.get(state)
    if cached is not None:
        return cached
    path = _TRAY_ICON_PATHS.get(state, _ICON_PATH)
    if not os.path.isfile(path):
        _log(f"tray icon for '{state}' missing at {path}; falling back to the logo")
        path = _ICON_PATH
    image = _load_image(path)
    _tray_image_cache[state] = image
    return image


def _cast_state() -> str:
    with _lock:
        proc = _proc
    return "casting" if proc is not None and proc.poll() is None else "idle"


def _glow_button_image(canvas_w: int, canvas_h: int, btn_w: int, btn_h: int,
                       radius: int, colour: str, bg: str,
                       scale: float = 1.0) -> Image.Image:
    
    box = ((canvas_w - btn_w) // 2, (canvas_h - btn_h) // 2,
           (canvas_w + btn_w) // 2, (canvas_h + btn_h) // 2)
    shape = Image.new("L", (canvas_w, canvas_h), 0)
    ImageDraw.Draw(shape).rounded_rectangle(box, radius=radius, fill=255)

    base = Image.new("RGB", (canvas_w, canvas_h), bg)
    tint = Image.new("RGB", (canvas_w, canvas_h), colour)
    rgb = tint.getpixel((0, 0))
    for blur, strength in ((5, 0.7), (14, 0.55)):
        halo = shape.filter(ImageFilter.GaussianBlur(blur * scale))
        halo = halo.point(lambda v, s=strength: int(v * s))
        glow = Image.merge("RGB", [halo.point(lambda v, c=c: v * c // 255)
                                   for c in rgb])
        base = ImageChops.add(base, glow)

    ss = 4
    crisp = Image.new("L", (canvas_w * ss, canvas_h * ss), 0)
    ImageDraw.Draw(crisp).rounded_rectangle(
        [c * ss for c in box], radius=radius * ss, fill=255)
    base.paste(tint, mask=crisp.resize((canvas_w, canvas_h), Image.LANCZOS))
    return base

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
        _notify(
            _l("FluxCast stopped"), 
            _l("Cast to {target} ended cleanly.").format(target=target)
        )
    else:
        _notify(
            _l("FluxCast stopped"), 
            _l("Cast to {target} exited (code {rc}).").format(target=target, rc=rc)
        )
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
    _notify(
        "FluxCast", 
        _l("Starting cast to {target}… (log: {path})").format(target=target, path=_LOG_PATH)
    )
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

def _profile_args(mode: str) -> list[str]:
    return load_profile(mode, warn=lambda message: _log(f"config warning: {message}"))


def _monitor_name(mode: str, selected) -> str | None:
    """Config `monitor` for `mode` if set, else the tray's selected output."""
    return load_preferred_monitor(
        mode, warn=lambda message: _log(f"config warning: {message}")
    ) or (selected.name if selected is not None else None)


def _start_wfd(peer, monitor) -> None:
    cmd = [_PY, _MAIN, "--protocol", "wfd", "--wfd-peer", peer.address]
    name = _monitor_name("wfd", monitor)
    # On non-Hyprland Wayland the WFD backend uses xdg-portal and shows its
    if name and not _wfd_uses_portal():
        cmd += ["--monitor", name]
    cmd += _profile_args("wfd")
    _launch(cmd, peer.name or peer.address)


def _start_dlna(device, monitor) -> None:
    cmd = [_PY, _MAIN, "--protocol", "dlna", "--device-name", device.friendly_name]
    name = _monitor_name("dlna", monitor)
    if name:
        cmd += ["--monitor", name]
    cmd += _profile_args("dlna")
    _launch(cmd, device.friendly_name)


def _start_cast(device, monitor) -> None:
    name = device.cast_info.friendly_name
    cmd = [_PY, _MAIN, "--protocol", "cast", "--device-name", name]
    mon = _monitor_name("cast", monitor)
    if mon:
        cmd += ["--monitor", mon]
    cmd += _profile_args("cast")
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

    def _get_font_scale() -> float:
        import configparser
        for path in ["~/.config/gtk-4.0/settings.ini", "~/.config/gtk-3.0/settings.ini"]:
            try:
                cfg = configparser.ConfigParser(strict=False)
                cfg.read(os.path.expanduser(path))
                font_name = cfg.get("Settings", "gtk-font-name", fallback=None)
                if font_name:
                    size = float(font_name.replace(",", " ").split()[-1])
                    if size > 0:
                        return size / 10.0
            except Exception:
                continue
        return 1.0

    _scale = _get_font_scale()
    version = get_fluxcast_version()

    def _run():
        try:
            import tkinter as tk
            from PIL import ImageTk
        except ImportError as exc:
            _log(f"About window unavailable: {exc}")
            _notify(_l("About FluxCast"),
                    _l("The About window needs the Tk libraries "
                       "(install the 'tk' package)."))
            return

        BG = "#0a120d"
        FG = "#e5e7eb"
        ACCENT = "#30D987"
        LINK = "#60a5fa"
        KOFI = "#ff5e5b"
        SEP = "#1e3a26"
        BTN_BG = "#1a2e1f"

        root = tk.Tk()
        root.title("About FluxCast")
        root.resizable(True, True)
        root.configure(bg=BG)
        try:
            root.attributes("-type", "dialog")
        except tk.TclError:
            pass

        def _link(parent, text, url, size, colour):
            font = ("sans-serif", int(size * _scale))
            lbl = tk.Label(parent, text=text, fg=colour, bg=BG, cursor="hand2", font=font)
            lbl.bind("<Enter>", lambda _: lbl.configure(font=font + ("underline",)))
            lbl.bind("<Leave>", lambda _: lbl.configure(font=font))
            lbl.bind("<Button-1>", lambda _: webbrowser.open(url))
            return lbl

        canvas = tk.Canvas(root, bg=BG, bd=0, highlightthickness=0)
        scrollbar = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)

        frame = tk.Frame(canvas, bg=BG, padx=int(28 * _scale), pady=int(20 * _scale))
        body = canvas.create_window(0, 0, window=frame, anchor="nw")

        try:
            img = Image.open(_ICON_PATH).resize((72, 72), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            tk.Label(frame, image=photo, bg=BG).pack(pady=(0, 8))
            root._photo = photo
            root.iconphoto(True, photo)
        except Exception:
            pass

        tk.Label(frame, text=f"FluxCast", font=("sans-serif", int(16 * _scale), "bold"),
                 bg=BG, fg=FG).pack()
        tk.Label(frame, text=version, font=("sans-serif", int(10 * _scale)),
                 bg=BG, fg=FG).pack(pady=(0, 8), anchor="center")
        tk.Label(frame, text=_l("Desktop → Smart TV streaming for Linux"),
                 font=("sans-serif", int(10 * _scale)), bg=BG, fg=ACCENT).pack(pady=(2, 14))

        tk.Frame(frame, bg=SEP, height=1).pack(fill="x", pady=(0, 12))

        story = _l(
            "I wanted to watch movies by streaming my Linux desktop to a TV — "
            "nothing worked. gnome-network-displays gave me one frame and then "
            "froze. miraclecast hasn't been meaningfully updated in years.\n\n"
            "So I built FluxCast from scratch: Wi-Fi Direct via NetworkManager, "
            "full RTSP handshake, RTP media stream. ~1 second latency, "
            "video and audio. It actually works."
        )
        tk.Label(frame, text=story, font=("sans-serif", int(10 * _scale)),
                 wraplength=int(360 * _scale), justify="left", bg=BG, fg=FG).pack(pady=(0, 14))

        tk.Frame(frame, bg=SEP, height=1).pack(fill="x", pady=(0, 10))

        for text, url in [
            ("fluxcast.dev", "https://fluxcast.dev/"),
            ("github.com/IlyaP358/fluxcast", _REPO),
        ]:
            _link(frame, text, url, 10, LINK).pack(anchor="center", expand=True)

        KOFI_W, KOFI_H, KOFI_R = 300, 60, 13
        KOFI_GLOW_PAD = 27
        KOFI_FONT = 11

        btn_w, btn_h = int(KOFI_W * _scale), int(KOFI_H * _scale)
        pad = int(KOFI_GLOW_PAD * _scale)
        kofi_w, kofi_h = btn_w + pad * 2, btn_h + pad * 2
        kofi_canvas = tk.Canvas(
            frame,
            width=kofi_w,
            height=kofi_h,
            bg=BG,
            bd=0,
            highlightthickness=0
        )
        kofi_canvas.pack(anchor="center", pady=(0, 0))

        kofi_frames = {
            colour: ImageTk.PhotoImage(_glow_button_image(
                kofi_w, kofi_h, btn_w, btn_h,
                int(KOFI_R * _scale), colour, BG, _scale))
            for colour in (KOFI, "#ff4a47")
        }
        kofi_canvas.kofi_frames = kofi_frames

        btn_image = kofi_canvas.create_image(
            kofi_w // 2, kofi_h // 2, image=kofi_frames[KOFI])
        kofi_canvas.create_text(
            kofi_w // 2, kofi_h // 2,
            text=_l("Help continue FluxCast development"),
            fill="#ffffff",
            font=("sans-serif", int(KOFI_FONT * _scale), "bold"))

        def on_enter(e): kofi_canvas.itemconfig(btn_image, image=kofi_frames["#ff4a47"])
        def on_leave(e): kofi_canvas.itemconfig(btn_image, image=kofi_frames[KOFI])
        def on_click(e): webbrowser.open("https://ko-fi.com/fluxcast")

        kofi_canvas.bind("<Enter>", on_enter)
        kofi_canvas.bind("<Leave>", on_leave)
        kofi_canvas.bind("<Button-1>", on_click)

        tk.Frame(frame, bg=SEP, height=1).pack(fill="x", pady=(12, 10))

        FG_DIM = "#6b7280"
        for text, url in [
            ("illia@fluxcast.dev", "mailto:illia@fluxcast.dev"),
            ("Join our Discord", "https://discord.gg/GCmPNpJZM7"),
            ("View Contributors", "https://fluxcast.dev/contributors.html"),
        ]:
            _link(frame, _l(text), url, 8, FG_DIM).pack()

        tk.Frame(frame, bg=SEP, height=1).pack(fill="x", pady=(12, 10))

        credit = _l("Author: IlyaP358  |  Code licensed under GPL-3.0")
        author, separator, licence = credit.partition("|")
        footer = tk.Frame(frame, bg=BG)
        footer.pack(pady=(8, 0))
        if separator:
            _link(footer, author.strip(), _REPO.rsplit("/", 1)[0], 7, FG_DIM).pack(side="left")
            tk.Label(footer, text=f"  {separator}  ", font=("sans-serif", int(7 * _scale)),
                     bg=BG, fg=FG_DIM).pack(side="left")
            _link(footer, licence.strip(), f"{_REPO}/blob/main/LICENSE", 7, FG_DIM).pack(side="left")
        else:
            _link(footer, credit, f"{_REPO}/blob/main/LICENSE", 7, FG_DIM).pack()

        tk.Button(frame, text=_l("Close"), command=root.destroy, width=10,
                  bg=BTN_BG, fg=FG, activebackground="#253d2a",
                  activeforeground=FG, relief="flat",
                  cursor="hand2").pack(pady=(12, 0))

        root.update_idletasks()
        natural_w, natural_h = frame.winfo_reqwidth(), frame.winfo_reqheight()
        # Leave room for the panel and title bar rather than filling the screen.
        height = min(natural_h, int(root.winfo_screenheight() * 0.85))
        scrolls = height < natural_h
        if scrolls:
            scrollbar.grid(row=0, column=1, sticky="ns")
        root.geometry(f"{natural_w + (scrollbar.winfo_reqwidth() if scrolls else 0)}x{height}")
        root.minsize(natural_w, int(240 * _scale))

        def _fit(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(body, width=canvas.winfo_width())
            if frame.winfo_reqheight() > canvas.winfo_height():
                scrollbar.grid(row=0, column=1, sticky="ns")
            else:
                scrollbar.grid_remove()

        def _wheel(event):
            if event.num == 4 or event.delta > 0:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5 or event.delta < 0:
                canvas.yview_scroll(1, "units")

        frame.bind("<Configure>", _fit)
        canvas.bind("<Configure>", _fit)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            root.bind_all(sequence, _wheel)

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
    status = _l("● Casting: {target}").format(target=target) if casting else _l("● Idle")
    items.append(pystray.MenuItem(status, None, enabled=False))
    items.append(pystray.Menu.SEPARATOR)

    if casting:
        items.append(pystray.MenuItem(_l("Stop Casting"), lambda *_: _stop()))
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
            items.append(pystray.MenuItem(_l("Monitor"), pystray.Menu(*mon_items)))
        elif sc_mon:
            items.append(pystray.MenuItem(_l("Monitor: scanning…"), None, enabled=False))

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
                peer_items = [pystray.MenuItem(_l("Select a monitor first"), None, enabled=False)]
            if sc_wfd:
                peer_items.append(pystray.MenuItem(_l("↻ Refreshing…"), None, enabled=False))
            wfd_sub = pystray.Menu(*peer_items)
        elif sc_wfd:
            wfd_sub = pystray.Menu(pystray.MenuItem(_l("Scanning…"), None, enabled=False))
        else:
            wfd_sub = pystray.Menu(pystray.MenuItem(_l("No WFD devices found"), None, enabled=False))
        wfd_label = _l("Cast via Miracast (WFD, portal dialog)   ") if wfd_via_portal else _l("Cast via Miracast (WFD)")
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
                dlna_items = [pystray.MenuItem(_l("Select a monitor first"), None, enabled=False)]
            if sc_dlna:
                dlna_items.append(pystray.MenuItem(_l("↻ Refreshing…"), None, enabled=False))
            dlna_sub = pystray.Menu(*dlna_items)
        elif sc_dlna:
            dlna_sub = pystray.Menu(pystray.MenuItem(_l("Scanning…"), None, enabled=False))
        else:
            dlna_sub = pystray.Menu(pystray.MenuItem(_l("No DLNA devices found"), None, enabled=False))
        items.append(pystray.MenuItem(_l("Cast via DLNA/UPnP"), dlna_sub))

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
                cast_items = [pystray.MenuItem(_l("Select a monitor first"), None, enabled=False)]
            if sc_cast:
                cast_items.append(pystray.MenuItem(_l("↻ Refreshing…"), None, enabled=False))
            cast_sub = pystray.Menu(*cast_items)
        elif sc_cast:
            cast_sub = pystray.Menu(pystray.MenuItem(_l("Scanning…"), None, enabled=False))
        else:
            cast_sub = pystray.Menu(pystray.MenuItem(_l("No Chromecast devices found"), None, enabled=False))
        items.append(pystray.MenuItem(_l("Cast via Chromecast"), cast_sub))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem(_l("Rescan Devices"), lambda *_: _rescan_all()))

    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem(_l("About FluxCast"), lambda *_: _show_about()))
    items.append(pystray.MenuItem(_l("Exit"), _on_exit))

    return items


def run_tray() -> None:
    global _icon, _icon_state
    _log("run_tray() starting")
    _icon_state = _cast_state()
    icon = pystray.Icon(
        "FluxCast",
        _tray_image(_icon_state),
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
