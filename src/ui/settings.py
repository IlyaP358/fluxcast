"""Tray settings window: edits ~/.config/fluxcast/config without a terminal.

Values are checked with the validators from tray_config, so the window cannot
write anything load_profile() would refuse.
"""

import configparser
import os
import subprocess
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from PIL import Image, ImageTk

from i18n import _l
from ui.tray_config import _MODE_OPTIONS, get_config_path


def _gsettings_dark():
    for key in ("color-scheme", "gtk-theme"):
        try:
            result = subprocess.run(
                ("gsettings", "get", "org.gnome.desktop.interface", key),
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if "dark" in result.stdout.lower():
            return True
    return False


def _gtk_prefs():
    """Font scale and dark mode from the GTK config, or gsettings if it is absent."""
    scale, dark = 1.0, None
    for path in ("~/.config/gtk-4.0/settings.ini", "~/.config/gtk-3.0/settings.ini"):
        cfg = configparser.ConfigParser(strict=False)
        try:
            if not cfg.read(os.path.expanduser(path)):
                continue
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue

        if dark is None:
            prefer = cfg.get("Settings", "gtk-application-prefer-dark-theme", fallback="")
            theme = cfg.get("Settings", "gtk-theme-name", fallback="")
            if prefer.strip().lower() in ("1", "true", "yes"):
                dark = True
            elif theme:
                dark = "dark" in theme.lower()

        if scale == 1.0:
            font = cfg.get("Settings", "gtk-font-name", fallback="")
            try:
                size = float(font.replace(",", " ").split()[-1])
            except (ValueError, IndexError):
                size = 0.0
            if size > 0:
                scale = size / 10.0

    return scale, _gsettings_dark() if dark is None else dark


_SCALE, _DARK = _gtk_prefs()

if _DARK:
    BG = "#1e1e1e"
    TEXT = "#ffffff"
    BORDER = "#3c3c3c"
    MUTED = "#818181"
    FIELD_BG = "#2d2d2d"
    BUTTON_BG = "#3c3c3c"
else:
    BG = "#ffffff"
    TEXT = "#3C3C3C"
    BORDER = "#e5e7eb"
    MUTED = "#818181"
    FIELD_BG = "#f9fafb"
    BUTTON_BG = "#dedede"

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ICON_PATH = os.path.join(_BASE, "assets", "flcast_logo_512x512.png")

_settings_thread = None

_TABS = (
    ("Miracast (WFD)", "wfd"),
    ("DLNA", "dlna"),
    ("Chromecast", "cast"),
)

_VIDEO = (
    ("output-res", "Output resolution", "resolution",
     "Empty keeps the desktop resolution"),
    ("fps", "Frames per second", "number", "Default 30"),
    ("bitrate", "Video bitrate", "text", "For example 4M or 12M. Default 4M"),
)

_GROUPS = {
    "wfd": (
        ("Video", _VIDEO + (
            ("wfd-encoder", "H.264 encoder", "choice",
             "va encodes on the GPU through VA-API and falls back to x264 when it is missing"),
            ("wfd-rate-control", "Rate control", "choice",
             "Only used with the va encoder. Default qvbr"),
        )),
        ("Audio", (
            ("wfd-no-audio", "Stream video only", "bool", ""),
            ("wfd-audio-device", "Audio source", "text",
             "PulseAudio or PipeWire monitor source. Empty picks one automatically"),
        )),
        ("Capture", (
            ("wfd-capture-backend", "Capture backend", "choice", "Default auto"),
            ("wfd-media-pipeline", "Media pipeline", "choice",
             "Which sender pushes the RTP stream. Default auto"),
            ("wfd-test-pattern", "Stream a generated test picture instead of the desktop",
             "bool", ""),
        )),
        ("Connection", (
            ("wfd-interface", "Wi-Fi interface", "text",
             "Used when scanning for TVs, for example wlan0. Empty picks one automatically"),
            ("wfd-rtsp-port", "RTSP port", "number", "Default 7236"),
            ("wfd-rtp-source-port", "RTP source port", "number", "Default 19002"),
            ("wfd-timeout", "Scan timeout", "number", "Wi-Fi Direct scan, in seconds. Default 8"),
            ("wfd-go-intent", "Group owner intent", "number",
             "0 to 15. 0 lets the TV own the group, which most Miracast TVs require. Default 0"),
            ("wfd-no-firewall", "Do not open the RTSP port in firewalld", "bool", ""),
        )),
        ("Advanced", (
            ("wfd-r2", "Ask the TV for WFD R2 formats, which can unlock 1080p60", "bool", ""),
            ("wfd-uibc", "Accept touch and mouse input back from the TV", "bool",
             "Experimental, and needs access to /dev/uinput"),
            ("wfd-aosp-pmt-pid", "Write MPEG-TS tables the way Android sinks expect them",
             "bool", "Try this when the session connects but the TV stays black"),
            ("wfd-latency-log", "Latency log", "text",
             "Path to a JSONL file written during the session. "
             "Default /tmp/fluxcast-wfd-latency.jsonl"),
        )),
    ),
    "dlna": (
        ("Video", _VIDEO),
        ("Server", (
            ("host", "Bind address", "text",
             "LAN address to serve and advertise. Empty detects it automatically"),
            ("port", "HTTP port", "number", "Default 8080"),
            ("transport", "Transport", "choice",
             "Default progressive-ts for DLNA and hls for Chromecast. live-ts is experimental"),
        )),
        ("Capture", (
            ("capture-backend", "Capture backend", "choice", "Default auto"),
            ("discover-timeout", "Discovery timeout", "number", "In seconds. Default 5"),
        )),
    ),
}
_GROUPS["cast"] = _GROUPS["dlna"]

_UNSET = "default"


def _read_section(mode):
    try:
        text = get_config_path().read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}

    values = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            continue
        if current != mode or not stripped or stripped[0] in "#;":
            continue
        for separator in (":", "="):
            if separator in stripped:
                key, _, raw = stripped.partition(separator)
                values[key.strip()] = raw.strip()
                break
    return values


def _apply_to_text(text, mode, values):
    """Edit [mode] line by line; configparser would drop the user's comments."""
    lines = text.splitlines()
    start = end = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        if start is None and stripped[1:-1].strip() == mode:
            start = index + 1
        elif start is not None:
            end = index
            break
    if start is None:
        lines.extend(("", f"[{mode}]"))
        start = end = len(lines)
    if end is None:
        end = len(lines)

    remaining = dict(values)
    kept = []
    for line in lines[start:end]:
        stripped = line.strip()
        key = ""
        if stripped and stripped[0] not in "#;":
            for separator in (":", "="):
                if separator in stripped:
                    key = stripped.partition(separator)[0].strip()
                    break
        if key not in remaining:
            kept.append(line)
            continue
        value = remaining.pop(key)
        if value is not None:
            kept.append(f"{key} = {value}")

    tail = len(kept)
    while tail > 0 and not kept[tail - 1].strip():
        tail -= 1
    new = [f"{key} = {value}" for key, value in remaining.items() if value is not None]
    kept[tail:tail] = new

    return "\n".join(lines[:start] + kept + lines[end:]).rstrip("\n") + "\n"


def _write_section(mode, values):
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = f"[{mode}]\n"
    path.write_text(_apply_to_text(text, mode, values), encoding="utf-8")


class _Section:
    def __init__(self, parent, mode, scale):
        self.mode = mode
        self.scale = scale
        self.readers = {}
        self.frame = tk.Frame(parent, bg=BG)
        self.frame.columnconfigure(1, weight=1)
        self.body = tkfont.Font(size=int(10 * scale))
        self.small = tkfont.Font(size=int(9 * scale))
        self._row = 0
        self._build()

    def _next(self):
        row, self._row = self._row, self._row + 1
        return row

    def _heading(self, text):
        row = self._next()
        pad = (int(16 * self.scale), int(2 * self.scale)) if row else (0, int(2 * self.scale))
        tk.Label(
            self.frame, text=text, bg=BG, fg=TEXT, font=self.body, anchor="w",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=pad)
        tk.Frame(self.frame, bg=BORDER, height=1).grid(
            row=self._next(), column=0, columnspan=3, sticky="ew", pady=(0, int(6 * self.scale))
        )

    def _label(self, text):
        row = self._next()
        tk.Label(
            self.frame, text=text, bg=BG, fg=TEXT, font=self.body, anchor="w",
        ).grid(row=row, column=0, sticky="w",
               padx=(int(16 * self.scale), int(12 * self.scale)),
               pady=int(4 * self.scale))
        holder = tk.Frame(self.frame, bg=BG)
        holder.grid(row=row, column=2, sticky="e", pady=int(4 * self.scale))
        return holder

    def _hint(self, text):
        if not text:
            return
        tk.Label(
            self.frame, text=text, bg=BG, fg=MUTED, font=self.small,
            anchor="w", justify="left", wraplength=int(520 * self.scale),
        ).grid(row=self._next(), column=0, columnspan=3, sticky="w",
               padx=(int(16 * self.scale), 0), pady=(0, int(6 * self.scale)))

    def _entry(self, parent, variable, width):
        return tk.Entry(
            parent, textvariable=variable, width=width, font=self.body,
            bg=FIELD_BG, fg=TEXT, bd=1, relief="solid",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=MUTED,
        )

    def _resolution(self, label, value, hint):
        holder = self._label(label)
        width_var, height_var = tk.StringVar(), tk.StringVar()
        if value and "x" in value.lower():
            left, _, right = value.lower().partition("x")
            width_var.set(left.strip())
            height_var.set(right.strip())

        self._entry(holder, width_var, 7).pack(side="left")
        tk.Label(holder, text="×", bg=BG, fg=MUTED, font=self.body).pack(
            side="left", padx=int(6 * self.scale)
        )
        self._entry(holder, height_var, 7).pack(side="left")
        self._hint(hint)

        def read():
            width, height = width_var.get().strip(), height_var.get().strip()
            return f"{width}x{height}" if (width or height) else None

        return read

    def _number(self, label, value, hint):
        holder = self._label(label)
        variable = tk.StringVar(value=value or "")

        def step(delta):
            try:
                current = int(variable.get().strip())
            except ValueError:
                current = 0
            variable.set(str(max(0, current + delta)))

        def arrow(text, delta):
            return tk.Button(
                holder, text=text, font=self.small, bg=FIELD_BG, fg=MUTED,
                bd=0, relief="solid", highlightthickness=0,
                activebackground=BORDER, cursor="hand2",
                command=lambda: step(delta),
            )

        arrow("▲", 1).pack(fill="x")
        number_entry = self._entry(holder, variable, 8)
        number_entry.configure(justify="center")
        number_entry.pack(fill="x", pady=1)
        arrow("▼", -1).pack(fill="x")
        self._hint(hint)
        return lambda: variable.get().strip() or None

    def _text(self, label, value, hint):
        holder = self._label(label)
        variable = tk.StringVar(value=value or "")
        self._entry(holder, variable, 32).pack(side="left")
        self._hint(hint)
        return lambda: variable.get().strip() or None

    def _choice(self, label, value, hint, choices):
        holder = self._label(label)
        options = (_UNSET,) + tuple(choices)
        variable = tk.StringVar(value=value if value in choices else _UNSET)
        menu = tk.OptionMenu(holder, variable, *options)
        menu.configure(
            bg=FIELD_BG, fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
            highlightthickness=1, highlightbackground=BORDER, bd=0, relief="flat",
            width=18, anchor="w", font=self.body, indicatoron=True,
        )
        menu["menu"].configure(bg=BG, fg=TEXT, font=self.body, bd=1, relief="solid")
        menu.pack(side="left")
        self._hint(hint)
        return lambda: None if variable.get() == _UNSET else variable.get()

    def _bool(self, label, value, hint):
        row = self._next()
        variable = tk.BooleanVar(
            value=str(value).strip().lower() in ("1", "yes", "true", "on")
        )
        tk.Checkbutton(
            self.frame, text=label, variable=variable, bg=BG, fg=TEXT, font=self.body,
            activebackground=BG, activeforeground=TEXT, selectcolor=BG,
            highlightthickness=0, bd=0, anchor="w", cursor="hand2",
        ).grid(row=row, column=0, columnspan=3, sticky="w",
               padx=(int(13 * self.scale), 0), pady=int(2 * self.scale))
        self._hint(hint)
        return lambda: "true" if variable.get() else None

    def _build(self):
        options = _MODE_OPTIONS[self.mode]
        current = _read_section(self.mode)

        for title, fields in _GROUPS[self.mode]:
            self._heading(_l(title))
            for key, label, kind, hint in fields:
                value = current.get(key, "")
                label, hint = _l(label), _l(hint) if hint else ""
                if kind == "resolution":
                    self.readers[key] = self._resolution(label, value, hint)
                elif kind == "number":
                    self.readers[key] = self._number(label, value, hint)
                elif kind == "bool":
                    self.readers[key] = self._bool(label, value, hint)
                elif kind == "choice":
                    choices = getattr(options[key].validate, "choices", ())
                    self.readers[key] = self._choice(label, value, hint, choices)
                else:
                    self.readers[key] = self._text(label, value, hint)

    def collect(self):
        options = _MODE_OPTIONS[self.mode]
        labels = {key: label
                  for _title, fields in _GROUPS[self.mode]
                  for key, label, _kind, _hint in fields}
        values, problems = {}, []

        for key, read in self.readers.items():
            raw = read()
            if raw is None:
                values[key] = None
                continue
            try:
                checked = options[key].validate(raw)
            except ValueError as exc:
                problems.append(f"{_l(labels[key])}: {exc}")
                continue
            values[key] = "true" if checked is True else str(checked)
        return values, problems


def _run():
    scale = _SCALE
    root = tk.Tk()
    root.title(_l("FluxCast Settings"))
    root.configure(bg=BG)
    root.resizable(True, True)
    try:
        root.attributes("-type", "dialog")
    except tk.TclError:
        pass

    tab_font = tkfont.Font(size=int(10 * scale))

    outer = tk.Frame(root, bg=BG)
    outer.pack(fill="both", expand=True)

    nav = tk.Frame(outer, bg=BG)
    nav.pack(fill="x", padx=int(14 * scale), pady=(int(10 * scale), 0))
    tk.Frame(outer, bg=BORDER, height=1).pack(fill="x")

    body = tk.Frame(outer, bg=BG)
    body.pack(fill="both", expand=True)

    footer = tk.Frame(outer, bg=BG)
    footer.pack(fill="x")
    tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", before=footer)

    tabs, sections, canvases, pages = {}, {}, {}, {}
    active = {"mode": _TABS[0][1]}

    def set_active_tab(mode):
        active["mode"] = mode
        for key, (button, underline) in tabs.items():
            chosen = key == mode
            button.configure(fg=TEXT if chosen else MUTED)
            underline.configure(bg=TEXT if chosen else BG)
        for key, holder in sections.items():
            if key == mode:
                holder.pack(fill="both", expand=True)
            else:
                holder.pack_forget()

    icon = None
    try:
        icon = ImageTk.PhotoImage(
            Image.open(_ICON_PATH).resize((int(20 * scale), int(20 * scale)), Image.LANCZOS)
        )
        tk.Label(nav, image=icon, bg=BG).pack(side="left", padx=(0, int(12 * scale)))
        root.iconphoto(True, icon)
    except Exception:
        pass
    root._fluxcast_icon = icon  # keep a reference so Tk does not drop the image

    for title, mode in _TABS:
        tab = tk.Frame(nav, bg=BG)
        tab.pack(side="left", padx=(0, int(4 * scale)))
        button = tk.Button(
            tab, text=_l(title), bg=BG, fg=MUTED, font=tab_font,
            padx=int(12 * scale), pady=int(8 * scale),
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            activebackground=BG, activeforeground=TEXT,
            command=lambda value=mode: set_active_tab(value),
        )
        button.pack()
        underline = tk.Frame(tab, bg=BG, height=2)
        underline.pack(fill="x")
        tabs[mode] = (button, underline)

        holder = tk.Frame(body, bg=BG)
        sections[mode] = holder
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        page = tk.Frame(canvas, bg=BG, padx=int(18 * scale), pady=int(12 * scale))
        window = canvas.create_window((0, 0), window=page, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        page.bind("<Configure>",
                  lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all")))
        canvas.bind("<Configure>",
                    lambda event, c=canvas, w=window: c.itemconfigure(w, width=event.width))
        canvases[mode] = canvas

        section = _Section(page, mode, scale)
        section.frame.pack(fill="both", expand=True)
        pages[mode] = section

    def _wheel(event):
        canvas = canvases.get(active["mode"])
        if canvas is not None:
            up = getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0
            canvas.yview_scroll(-1 if up else 1, "units")

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(sequence, _wheel)

    def apply_settings():
        mode = active["mode"]
        values, problems = pages[mode].collect()
        if problems:
            messagebox.showerror(_l("Invalid value"), "\n".join(problems))
            return
        try:
            _write_section(mode, values)
        except OSError as exc:
            messagebox.showerror(_l("Could not save"), str(exc))
            return
        messagebox.showinfo(
            _l("Settings"), _l("Saved. The new values apply to the next cast.")
        )

    def button(parent, text, command):
        return tk.Button(
            parent, text=text, bg=BUTTON_BG, fg=TEXT, font=tab_font,
            padx=int(14 * scale), pady=int(5 * scale),
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            activebackground=BORDER, activeforeground=TEXT, command=command,
        )

    button(footer, _l("Close"), root.destroy).pack(
        side="right", padx=(0, int(14 * scale)), pady=int(10 * scale)
    )
    button(footer, _l("Apply"), apply_settings).pack(
        side="right", padx=(0, int(8 * scale)), pady=int(10 * scale)
    )

    set_active_tab(_TABS[0][1])

    # Fixed size: asking for the full page height opened the window maximised.
    root.update_idletasks()
    width = min(int(300 * scale), int(root.winfo_screenwidth() * 0.9))
    height = min(int(840 * scale), int(root.winfo_screenheight() * 0.85))
    root.geometry(f"{width}x{height}")
    root.minsize(int(560 * scale), int(360 * scale))

    try:
        root.attributes("-type", "dialog")
    except tk.TclError:
        pass

    root.mainloop()


def show_settings() -> None:
    global _settings_thread
    if _settings_thread is not None and _settings_thread.is_alive():
        return
    _settings_thread = threading.Thread(target=_run, daemon=True)
    _settings_thread.start()
