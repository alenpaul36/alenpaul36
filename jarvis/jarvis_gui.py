#!/usr/bin/env python3
"""
Jarvis desktop app: a point-and-click window for jarvis.py.

    python jarvis_gui.py

Pages: Jarvis (talk to it; British male voice), Dashboard (live gauges),
Processes, Network, Storage, Services and Ask Jarvis (type requests).
Built on Tkinter, which ships with Python.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import platform
import queue
import random
import re
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import psutil

import assistant
import jarvis
import voice

jarvis.USE_COLOR = False

# --------------------------------------------------------------------------- #
# Look and feel
# --------------------------------------------------------------------------- #

BG = "#0b1220"
PANEL = "#111a2e"
PANEL_2 = "#17223b"
LINE = "#24314f"
TEXT = "#e2e8f0"
DIM = "#8b9ab4"
ACCENT = "#22d3ee"
GREEN = "#22c55e"
YELLOW = "#eab308"
RED = "#ef4444"

if sys.platform == "win32":
    FONT = "Segoe UI"
    MONO = "Consolas"
elif sys.platform == "darwin":
    FONT = "Helvetica Neue"
    MONO = "Menlo"
else:
    FONT = "DejaVu Sans"
    MONO = "DejaVu Sans Mono"


def level_color(pct: float) -> str:
    return GREEN if pct < 70 else YELLOW if pct < 90 else RED


def set_dpi_awareness() -> None:
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)


# --------------------------------------------------------------------------- #
# Small building blocks
# --------------------------------------------------------------------------- #

class Gauge(tk.Canvas):
    """A ring gauge showing a percentage."""

    def __init__(self, master, title: str, size: int = 150):
        super().__init__(master, width=size, height=size + 26, bg=PANEL, highlightthickness=0)
        self.size, self.title = size, title
        pad, w = 12, 12
        self.create_oval(pad, pad, size - pad, size - pad, outline=LINE, width=w)
        self.arc = self.create_arc(pad, pad, size - pad, size - pad, start=90, extent=0,
                                   style="arc", outline=ACCENT, width=w)
        self.value = self.create_text(size / 2, size / 2 - 6, text="--", fill=TEXT, font=(FONT, 20, "bold"))
        self.sub = self.create_text(size / 2, size / 2 + 18, text="", fill=DIM, font=(FONT, 9))
        self.create_text(size / 2, size + 12, text=title, fill=DIM, font=(FONT, 10, "bold"))

    def set(self, pct: float | None, sub: str = "") -> None:
        if pct is None:
            self.itemconfigure(self.arc, extent=0)
            self.itemconfigure(self.value, text="n/a")
        else:
            self.itemconfigure(self.arc, extent=-max(0.1, pct) * 3.6, outline=level_color(pct))
            self.itemconfigure(self.value, text=f"{pct:.0f}%")
        self.itemconfigure(self.sub, text=sub)


def panel(master, **kw) -> tk.Frame:
    return tk.Frame(master, bg=PANEL, highlightbackground=LINE, highlightthickness=1, **kw)


def label(master, text="", size=10, color=TEXT, bold=False, bg=None, **kw) -> tk.Label:
    return tk.Label(master, text=text, fg=color, bg=bg or master["bg"],
                    font=(FONT, size, "bold" if bold else "normal"), **kw)


def button(master, text, command, kind="normal") -> tk.Button:
    colors = {"normal": (PANEL_2, TEXT), "accent": (ACCENT, BG), "danger": ("#3b1620", "#fca5a5")}
    bg, fg = colors[kind]
    b = tk.Button(master, text=text, command=command, bg=bg, fg=fg, activebackground=LINE,
                  activeforeground=TEXT, relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
                  font=(FONT, 10, "bold" if kind == "accent" else "normal"), highlightthickness=0)
    return b


def make_table(master, columns: list[tuple[str, int]], height=14) -> ttk.Treeview:
    frame = tk.Frame(master, bg=PANEL)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=[c for c, _ in columns], show="headings", height=height)
    for name, width in columns:
        tree.heading(name, text=name)
        tree.column(name, width=width, anchor="w", stretch=name == columns[-1][0] or width > 200)
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return tree


def fill_table(tree: ttk.Treeview, rows) -> None:
    tree.delete(*tree.get_children())
    for r in rows:
        tree.insert("", "end", values=r)


class QueueWriter:
    """File-like object that forwards printed text to the GUI thread."""

    def __init__(self, app: "JarvisApp"):
        self.app = app

    def write(self, s: str) -> int:
        if s:
            self.app.ui(lambda s=s: self.app.console_write(s))
        return len(s)

    def flush(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

SETTINGS_FILE = Path.home() / ".jarvis.json"
DEFAULT_SETTINGS = {"voice": voice.DEFAULT_VOICE, "address": "sir", "wake_word": False, "speak": True}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    with contextlib.suppress(OSError, ValueError):
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    return settings


def save_settings(settings: dict) -> None:
    with contextlib.suppress(OSError):
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


YES = re.compile(r"\b(yes|yeah|yep|yup|sure|go ahead|do it|affirmative|confirm(ed)?|ok(ay)?|please do|proceed|absolutely|of course)\b")
NO = re.compile(r"\b(no|nope|don'?t|do not|cancel|stop|negative|never ?mind|leave it|wait)\b")


class GuiContext(assistant.Context):
    """Lets the assistant speak, ask and show pages inside the window."""

    def __init__(self, app: "JarvisApp"):
        self.app = app

    @property
    def address(self) -> str:
        return self.app.address

    def say(self, text: str) -> None:
        self.app.ui(lambda: self.app.reply(text))
        if self.app.settings["speak"]:
            self.app.speaker.say(text)

    def confirm(self, question: str) -> bool:
        self.say(question)
        if self.app._via_voice:
            with contextlib.suppress(voice.VoiceError):
                self.app.ui(lambda: self.app.set_state("listening", "Say yes or no…"))
                answer = (self.app.listener.listen(wait=7, phrase_limit=5) or "").lower()
                self.app.ui(lambda: self.app.set_state("thinking"))
                if answer:
                    self.app.ui(lambda: self.app.hud_heard.configure(text=f"You:  {answer}"))
                if NO.search(answer):
                    return False
                if YES.search(answer):
                    return True
        return self.app.confirm_from_thread(question)

    def show(self, page: str) -> None:
        self.app.ui(lambda: self.app.show(page))

    def output(self, argv: list[str]) -> None:
        self.app.console_target = self.app.console
        try:
            args = jarvis.build_parser().parse_args(argv)
            with contextlib.redirect_stdout(QueueWriter(self.app)):
                args.func(args)
        except SystemExit:
            pass

    def stop_speaking(self) -> None:
        self.app.speaker.stop()


class JarvisApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("J.A.R.V.I.S.")
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.configure(bg=BG)
        self._queue: queue.Queue = queue.Queue()
        self._busy = threading.Lock()
        self._net_prev = (psutil.net_io_counters(), time.time())
        self._style()

        jarvis.confirm = self.confirm_from_thread  # dialogs instead of terminal prompts
        jarvis.VOICE = jarvis.Voice(False)

        self.settings = load_settings()
        self.address = self.settings["address"]
        self.speaker = voice.Speaker(self.settings["voice"])
        self.listener = voice.Listener()
        self.ctx = GuiContext(self)
        self.jarvis = assistant.Assistant(self.ctx)
        self.latest: dict = {}
        self.level, self.level_target, self.state = 0.0, 0.0, "idle"
        self.angle = 0.0
        self._ptt = threading.Event()
        self._via_voice = False
        self.speaker.on_level = self._set_level
        self.listener.on_level = self._set_level
        self.speaker.on_state = lambda speaking: self.ui(
            lambda: self.set_state("speaking" if speaking else "idle"))

        self.sidebar = tk.Frame(self, bg=PANEL, width=210)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="left", fill="both", expand=True, padx=18, pady=16)

        label(self.sidebar, "J.A.R.V.I.S.", 17, ACCENT, True).pack(anchor="w", padx=20, pady=(22, 0))
        label(self.sidebar, "System manager", 9, DIM).pack(anchor="w", padx=20, pady=(0, 18))

        self.pages: dict[str, tk.Frame] = {}
        self.nav: dict[str, tk.Button] = {}
        for name, builder in [
            ("Jarvis", self.build_hud),
            ("Dashboard", self.build_dashboard),
            ("Processes", self.build_processes),
            ("Network", self.build_network),
            ("Storage", self.build_storage),
            ("Services", self.build_services),
            ("Ask Jarvis", self.build_console),
        ]:
            page = tk.Frame(self.content, bg=BG)
            self.pages[name] = page
            builder(page)
            b = tk.Button(self.sidebar, text="   " + name, anchor="w", relief="flat", bd=0, padx=10, pady=10,
                          bg=PANEL, fg=DIM, activebackground=PANEL_2, activeforeground=TEXT,
                          font=(FONT, 11), cursor="hand2", highlightthickness=0,
                          command=lambda n=name: self.show(n))
            b.pack(fill="x", padx=10, pady=1)
            self.nav[name] = b

        u = platform.uname()
        label(self.sidebar, f"{u.node}\n{u.system} {u.release}"[:60], 8, DIM, justify="left",
              wraplength=180).pack(side="bottom", anchor="w", padx=20, pady=16)

        self.show("Jarvis")
        self.bind_all("<Control-space>", lambda _: self.talk())
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(100, self._drain)
        self.after(200, self.refresh_dashboard)
        self.after(50, self._animate)
        threading.Thread(target=self._voice_worker, daemon=True).start()
        self.after(700, lambda: self.background(lambda: self.ctx.say(assistant.greeting(self.address))))

    # ---- plumbing --------------------------------------------------------- #

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    rowheight=26, borderwidth=0, font=(FONT, 10), bordercolor=PANEL,
                    lightcolor=PANEL, darkcolor=PANEL)
        s.configure("Treeview.Heading", background=PANEL_2, foreground=DIM, relief="flat",
                    font=(FONT, 9, "bold"), padding=6)
        s.map("Treeview", background=[("selected", "#164e63")], foreground=[("selected", TEXT)])
        s.map("Treeview.Heading", background=[("active", LINE)])
        for name in ("TScrollbar", "Vertical.TScrollbar"):
            s.configure(name, background=LINE, troughcolor=PANEL, bordercolor=PANEL, arrowcolor=DIM,
                        lightcolor=LINE, darkcolor=LINE, gripcount=0, arrowsize=12)
        s.map("Vertical.TScrollbar", background=[("active", "#33456b")])
        s.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2, foreground=TEXT,
                    arrowcolor=DIM, bordercolor=LINE, lightcolor=PANEL_2, darkcolor=PANEL_2)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL_2)], foreground=[("readonly", TEXT)])

    def ui(self, fn) -> None:
        """Schedule fn on the Tk thread (safe to call from workers)."""
        self._queue.put(fn)

    def _drain(self):
        try:
            while True:
                self._queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(60, self._drain)

    def background(self, work, done=None) -> None:
        def run():
            try:
                result = work()
            except Exception as e:  # surface instead of dying silently
                self.ui(lambda e=e: messagebox.showerror("Jarvis", str(e)))
                return
            if done:
                self.ui(lambda: done(result))

        threading.Thread(target=run, daemon=True).start()

    def confirm_from_thread(self, question: str, assume_yes: bool = False) -> bool:
        if assume_yes:
            return True
        if threading.current_thread() is threading.main_thread():
            return messagebox.askyesno("Jarvis", question)
        answer, ev = [False], threading.Event()

        def ask():
            answer[0] = messagebox.askyesno("Jarvis", question)
            ev.set()

        self.ui(ask)
        ev.wait()
        return answer[0]

    def quit_app(self):
        self.listener.cancel()
        self.speaker.stop()
        self.destroy()

    def show(self, name: str) -> None:
        for n, p in self.pages.items():
            p.pack_forget()
            self.nav[n].configure(bg=PANEL, fg=DIM)
        self.pages[name].pack(fill="both", expand=True)
        self.nav[name].configure(bg=PANEL_2, fg=ACCENT)
        self.current = name
        if name == "Processes":
            self.refresh_processes()
        elif name == "Network":
            self.refresh_network()
        elif name == "Storage":
            self.refresh_disks()

    def header(self, page, title, subtitle) -> tk.Frame:
        h = tk.Frame(page, bg=BG)
        h.pack(fill="x", pady=(0, 12))
        label(h, title, 18, TEXT, True).pack(anchor="w")
        label(h, subtitle, 10, DIM).pack(anchor="w")
        return h

    # ---- Jarvis (voice) --------------------------------------------------- #

    def build_hud(self, page):
        top = tk.Frame(page, bg=BG)
        top.pack(fill="x")
        label(top, "J.A.R.V.I.S.", 18, TEXT, True).pack(side="left")
        self.hud_clock = label(top, "", 11, DIM)
        self.hud_clock.pack(side="right")

        self.hud = tk.Canvas(page, bg=BG, highlightthickness=0, height=430)
        self.hud.pack(fill="both", expand=True)
        self._hud_items: dict = {}
        self.hud.bind("<Configure>", lambda e: self._draw_hud())
        self.hud.bind("<Button-1>", lambda e: self.talk())

        self.hud_status = label(page, "", 13, ACCENT, True)
        self.hud_status.pack(pady=(2, 2))
        self.hud_heard = label(page, "", 10, DIM, wraplength=760)
        self.hud_heard.pack()
        self.hud_reply = label(page, "", 11, TEXT, wraplength=760, justify="center")
        self.hud_reply.pack(pady=(2, 10))

        controls = tk.Frame(page, bg=BG)
        controls.pack()
        self.talk_btn = button(controls, "🎤  Talk to Jarvis", self.talk, "accent")
        self.talk_btn.configure(font=(FONT, 12, "bold"), padx=22, pady=10)
        self.talk_btn.pack(side="left", padx=(0, 14))
        self.typed = tk.StringVar()
        e = tk.Entry(controls, textvariable=self.typed, bg=PANEL_2, fg=TEXT, insertbackground=ACCENT, relief="flat",
                     highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT, font=(FONT, 11), width=34)
        e.pack(side="left", ipady=8)
        e.bind("<Return>", lambda _: (self.ask(self.typed.get(), stay=True), self.typed.set("")))
        label(controls, "  or type and press Enter", 9, DIM).pack(side="left")

        opts = tk.Frame(page, bg=BG)
        opts.pack(pady=(10, 0))

        def check(text, key, command=None):
            var = tk.BooleanVar(value=self.settings[key])

            def changed():
                self.settings[key] = var.get()
                save_settings(self.settings)
                if command:
                    command()

            tk.Checkbutton(opts, text=text, variable=var, command=changed, bg=BG, fg=DIM, selectcolor=PANEL_2,
                           activebackground=BG, activeforeground=TEXT, font=(FONT, 10), highlightthickness=0,
                           bd=0).pack(side="left", padx=10)
            return var

        self.wake_var = check('Always listen for "Jarvis"', "wake_word", self._wake_changed)
        check("Speak replies", "speak")
        label(opts, "Voice", 10, DIM).pack(side="left", padx=(14, 4))
        names = list(voice.BRITISH_VOICES)
        current = next((n for n, v in voice.BRITISH_VOICES.items() if v == self.settings["voice"]), names[0])
        vbox = ttk.Combobox(opts, values=names, state="readonly", width=20)
        vbox.set(current)
        vbox.pack(side="left")

        def voice_changed(_):
            self.settings["voice"] = voice.BRITISH_VOICES[vbox.get()]
            self.speaker.voice = self.settings["voice"]
            save_settings(self.settings)
            self.background(lambda: self.ctx.say(f"Voice updated, {self.address}. How do I sound?"))

        vbox.bind("<<ComboboxSelected>>", voice_changed)
        label(opts, "Call me", 10, DIM).pack(side="left", padx=(14, 4))
        abox = ttk.Combobox(opts, values=["sir", "ma'am", "boss", "chief"], width=10)
        abox.set(self.address)
        abox.pack(side="left")

        def address_changed(_=None):
            self.address = abox.get().strip() or "sir"
            self.settings["address"] = self.address
            save_settings(self.settings)

        abox.bind("<<ComboboxSelected>>", address_changed)
        abox.bind("<FocusOut>", address_changed)
        abox.bind("<Return>", address_changed)
        self.set_state("idle")

    def _draw_hud(self):
        c = self.hud
        c.delete("all")
        w, h = max(c.winfo_width(), 400), max(c.winfo_height(), 300)
        cx, cy = w / 2, h / 2
        R = min(w, h) / 2 - 18
        self._hud_geom = (cx, cy, R)
        it = self._hud_items = {"rings": []}
        # tick ring
        for i in range(120):
            a = math.radians(i * 3)
            r1 = R - (10 if i % 10 == 0 else 5)
            c.create_line(cx + R * math.cos(a), cy + R * math.sin(a), cx + r1 * math.cos(a), cy + r1 * math.sin(a),
                          fill="#1e3a5f" if i % 10 else "#2b5a85", width=1)
        # rotating segmented rings: (radius, width, segments, extent, speed, colour)
        for radius, width, segs, extent, speed, col in [
            (R * 0.90, 3, 4, 55, 0.5, "#22d3ee"),
            (R * 0.80, 9, 3, 75, -0.9, "#0e7490"),
            (R * 0.71, 2, 12, 18, 1.4, "#67e8f9"),
            (R * 0.64, 5, 2, 140, -0.35, "#155e75"),
        ]:
            ids = [c.create_arc(cx - radius, cy - radius, cx + radius, cy + radius, start=i * 360 / segs,
                                extent=extent, style="arc", outline=col, width=width) for i in range(segs)]
            it["rings"].append((ids, segs, speed, col))
        c.create_oval(cx - R * 0.56, cy - R * 0.56, cx + R * 0.56, cy + R * 0.56, outline="#164e63", width=1)
        it["glow"] = [c.create_oval(0, 0, 0, 0, fill=f, outline="") for f in ("#06202c", "#083344", "#0e4f66")]
        it["core"] = c.create_oval(0, 0, 0, 0, fill="#0891b2", outline="#67e8f9", width=2)
        it["name"] = c.create_text(cx, cy, text="JARVIS", fill="#ecfeff", font=(FONT, max(12, int(R / 11)), "bold"))
        # read-outs around the ring
        it["read"] = {}
        corners = {"cpu": (24, 18, "nw"), "mem": (w - 24, 18, "ne"), "disk": (24, h - 18, "sw"),
                   "net": (w - 24, h - 18, "se")}
        for key, (x, y, anchor) in corners.items():
            it["read"][key] = c.create_text(x, y, text="", fill=DIM, font=(MONO, 10), anchor=anchor,
                                            justify="left" if "w" in anchor else "right")
        self._update_readouts()

    def _update_readouts(self):
        it = self._hud_items.get("read")
        if not it:
            return
        L = self.latest
        fmt = lambda v: "--" if v is None else f"{v:4.0f}%"
        self.hud.itemconfigure(it["cpu"], text=f"CPU   {fmt(L.get('cpu'))}\n{self._spark(L.get('cpu'))}")
        self.hud.itemconfigure(it["mem"], text=f"{fmt(L.get('mem'))}   MEMORY\n{self._spark(L.get('mem'))}")
        self.hud.itemconfigure(it["disk"], text=f"DISK  {fmt(L.get('disk'))}\n{self._spark(L.get('disk'))}")
        up, down = L.get("up"), L.get("down")
        net = f"↑ {jarvis.human(up)}/s  ↓ {jarvis.human(down)}/s" if up is not None else "--"
        self.hud.itemconfigure(it["net"], text=f"NETWORK\n{net}")
        self.hud_clock.configure(text=dt.datetime.now().strftime("%A %d %B  ·  %H:%M"))

    @staticmethod
    def _spark(v) -> str:
        if v is None:
            return ""
        n = int(round(v / 10))
        return "▰" * n + "▱" * (10 - n)

    def _set_level(self, v: float) -> None:
        self.level_target = max(0.0, min(1.0, v))

    def _animate(self):
        it = self._hud_items
        active = it and self.current == "Jarvis" and self.wm_state() != "iconic"
        if active:
            speed = {"idle": 1.0, "listening": 2.6, "thinking": 4.5, "speaking": 1.6}.get(self.state, 1)
            self.angle += speed
            self.level += (self.level_target - self.level) * 0.35
            cx, cy, R = self._hud_geom
            for ids, segs, sp, _ in it["rings"]:
                for i, item in enumerate(ids):
                    self.hud.itemconfigure(item, start=(self.angle * sp + i * 360 / segs) % 360)
            breathe = 0.04 * math.sin(time.time() * 2.2) if self.state == "idle" else 0
            r = R * (0.30 + breathe + 0.14 * self.level)
            for k, g in enumerate(it["glow"]):
                gr = r * (1.75 - k * 0.25) + self.level * R * 0.1
                self.hud.coords(g, cx - gr, cy - gr, cx + gr, cy + gr)
            self.hud.coords(it["core"], cx - r, cy - r, cx + r, cy + r)
            core = {"idle": "#0891b2", "listening": "#0d9488", "thinking": "#b45309", "speaking": "#06b6d4"}
            self.hud.itemconfigure(it["core"], fill=core.get(self.state, "#0891b2"))
        self.after(33 if active else 300, self._animate)  # ~30 fps only while visible

    def set_state(self, state: str, text: str | None = None) -> None:
        self.state = state
        default = {
            "idle": 'Say "Jarvis" or press Talk' if self.settings["wake_word"] else "Press Talk or Ctrl+Space to speak",
            "listening": "Listening…", "thinking": "Working on it…", "speaking": "",
        }
        self.hud_status.configure(text=text if text is not None else default.get(state, ""),
                                  fg={"listening": "#5eead4", "thinking": YELLOW}.get(state, ACCENT))
        if hasattr(self, "talk_btn"):
            self.talk_btn.configure(text="■  Stop" if state in ("listening", "speaking") else "🎤  Talk to Jarvis")

    def reply(self, text: str) -> None:
        self.hud_reply.configure(text=text)
        self.console_target = self.console
        self.console_write(f"Jarvis: {text}\n", ACCENT)

    def talk(self):
        if self.state == "speaking":
            return self.speaker.stop()
        if self.state == "listening" and not self.settings["wake_word"]:
            return self.listener.cancel()
        self.show("Jarvis")
        self._ptt.set()
        self.listener.cancel()  # interrupt wake-word listening so the request is heard directly

    def _wake_changed(self):
        self.set_state(self.state)
        self.listener.cancel()

    def _voice_worker(self):
        """Background loop: push-to-talk requests and, if enabled, the "Jarvis" wake word."""
        while True:
            try:
                if self._ptt.is_set():
                    self._ptt.clear()
                    self._converse()
                elif self.settings["wake_word"] and self.state in ("idle",):
                    self.ui(lambda: self.set_state("idle"))
                    heard = self.listener.listen(wait=8, phrase_limit=10)
                    if heard and re.search(r"\bjarvis\b", heard, re.I) and not self._ptt.is_set():
                        rest = re.split(r"\bjarvis\b[\s,.!?]*", heard, maxsplit=1, flags=re.I)[1].strip()
                        if rest:
                            self._run_heard(rest)
                        else:
                            self._via_voice = True
                            self.ctx.say(random.choice([f"Yes, {self.address}?", f"At your service, {self.address}.",
                                                        f"{self.address.capitalize()}?"]))
                            self._converse()
                else:
                    self._ptt.wait(0.25)
            except voice.VoiceError as e:
                self.settings["wake_word"] = False
                self.ui(lambda e=e: (self.wake_var.set(False), self.set_state("idle", f"Voice unavailable: {e}")))
                time.sleep(1)
            except Exception as e:  # keep the loop alive
                self.ui(lambda e=e: self.set_state("idle", f"Voice error: {e}"))
                time.sleep(1)

    def _converse(self):
        self.ui(lambda: self.set_state("listening"))
        heard = self.listener.listen(wait=7, phrase_limit=12)
        if heard is None:
            return self.ui(lambda: self.set_state("idle"))
        if heard == "":
            self._via_voice = True
            return self.ctx.say(f"I'm sorry, {self.address}, I didn't catch that.")
        self._run_heard(heard)

    def _run_heard(self, text: str):
        self.ui(lambda: (self.hud_heard.configure(text=f"You:  {text}"),
                         setattr(self, "console_target", self.console),
                         self.console_write(f"\nyou (voice) › {text}\n", GREEN)))
        self._via_voice = True
        self.ui(lambda: self.set_state("thinking"))
        try:
            self.jarvis.handle(text)
        finally:
            self.ui(lambda: self.set_state("idle") if self.state == "thinking" else None)

    # ---- Dashboard -------------------------------------------------------- #

    def build_dashboard(self, page):
        h = self.header(page, "Dashboard", "Live view of your computer, refreshed every 2 seconds.")
        self.greeting = label(h, "", 10, ACCENT)
        self.greeting.pack(anchor="w", pady=(6, 0))

        row = panel(page)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=PANEL)
        inner.pack(pady=14)
        self.g_cpu = Gauge(inner, "CPU")
        self.g_mem = Gauge(inner, "MEMORY")
        self.g_disk = Gauge(inner, "MAIN DISK")
        self.g_bat = Gauge(inner, "BATTERY")
        for g in (self.g_cpu, self.g_mem, self.g_disk, self.g_bat):
            g.pack(side="left", padx=18)

        mid = tk.Frame(page, bg=BG)
        mid.pack(fill="both", expand=True, pady=12)

        info = panel(mid)
        info.pack(side="left", fill="both", expand=True, padx=(0, 6))
        label(info, "SYSTEM", 9, DIM, True).pack(anchor="w", padx=14, pady=(12, 4))
        self.info_lbl = label(info, "", 10, TEXT, justify="left", anchor="w")
        self.info_lbl.pack(anchor="w", fill="x", padx=14)
        label(info, "ALERTS", 9, DIM, True).pack(anchor="w", padx=14, pady=(14, 4))
        self.alert_lbl = label(info, "", 10, GREEN, justify="left", anchor="w", wraplength=380)
        self.alert_lbl.pack(anchor="w", fill="x", padx=14, pady=(0, 12))

        top = panel(mid)
        top.pack(side="left", fill="both", expand=True, padx=(6, 0))
        label(top, "BUSIEST PROGRAMS", 9, DIM, True).pack(anchor="w", padx=14, pady=(12, 4))
        self.dash_top = tk.Frame(top, bg=PANEL)
        self.dash_top.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        actions = tk.Frame(page, bg=BG)
        actions.pack(fill="x")
        button(actions, "Save system report", self.save_report, "accent").pack(side="left", padx=(0, 8))
        button(actions, "Check internet", self.check_internet).pack(side="left", padx=8)
        button(actions, "Clean temp files", lambda: self.show("Storage")).pack(side="left", padx=8)
        button(actions, "Ask Jarvis", lambda: self.show("Ask Jarvis")).pack(side="left", padx=8)

    def refresh_dashboard(self):
        on_dashboard = self.current == "Dashboard"

        def collect():
            try:
                return _collect()
            except Exception:
                return None

        def _collect():
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            root = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
            du = psutil.disk_usage(root)
            bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
            now = time.time()
            net = psutil.net_io_counters()
            prev, t0 = self._net_prev
            self._net_prev = (net, now)
            dt_s = max(now - t0, 0.001)
            up = (net.bytes_sent - prev.bytes_sent) / dt_s
            down = (net.bytes_recv - prev.bytes_recv) / dt_s
            procs, nprocs = [], 0
            if on_dashboard:  # the full process scan is the expensive part; skip it when not shown
                ncpu = psutil.cpu_count() or 1
                for p, info in jarvis.processes(["name", "memory_percent"]):
                    with contextlib.suppress(psutil.Error):
                        procs.append((p.cpu_percent(None) / ncpu, info["memory_percent"] or 0, info["name"] or "?"))
                nprocs = len(procs)
                procs.sort(reverse=True)
            alerts = []
            if cpu > 90: alerts.append("CPU is running very high.")
            if vm.percent > 90: alerts.append("Memory is nearly full.")
            for part in jarvis._disks():
                with contextlib.suppress(OSError):
                    if psutil.disk_usage(part.mountpoint).percent > 90:
                        alerts.append(f"Disk {part.mountpoint} is over 90% full.")
            if bat and not bat.power_plugged and bat.percent < 20:
                alerts.append("Battery is low; plug in soon.")
            return cpu, vm, du, root, bat, up, down, procs[:6], nprocs, alerts

        def show(r):
            self.after(2000, self.refresh_dashboard)
            if r is None:
                return
            cpu, vm, du, root, bat, up, down, procs, nprocs, alerts = r
            self.latest = {"cpu": cpu, "mem": vm.percent, "disk": du.percent, "up": up, "down": down,
                           "bat": bat.percent if bat else None, "alerts": alerts}
            self._update_readouts()
            if not on_dashboard:
                return
            self.g_cpu.set(cpu, f"{psutil.cpu_count()} threads")
            self.g_mem.set(vm.percent, f"{jarvis.human(vm.used)} / {jarvis.human(vm.total)}")
            self.g_disk.set(du.percent, f"{jarvis.human(du.free)} free")
            if bat:
                self.g_bat.set(bat.percent, "charging" if bat.power_plugged else "on battery")
            else:
                self.g_bat.set(None, "no battery")
            u = platform.uname()
            self.info_lbl.configure(text=(
                f"Computer      {u.node}\n"
                f"System        {u.system} {u.release} ({u.machine})\n"
                f"Up for        {jarvis.uptime()}\n"
                f"Network       ↑ {jarvis.human(up)}/s    ↓ {jarvis.human(down)}/s\n"
                f"Processes     {nprocs}"))
            self.alert_lbl.configure(text="\n".join(alerts) if alerts else "All systems nominal.",
                                     fg=YELLOW if alerts else GREEN)
            hr = dt.datetime.now().hour
            part = "morning" if hr < 12 else "afternoon" if hr < 18 else "evening"
            self.greeting.configure(text=f"Good {part}. {dt.datetime.now():%A %d %B, %H:%M}")
            for w in self.dash_top.winfo_children():
                w.destroy()
            for cpu_p, mem_p, name in procs:
                row = tk.Frame(self.dash_top, bg=PANEL)
                row.pack(fill="x", pady=2)
                label(row, name[:28], 10, TEXT, width=24, anchor="w").pack(side="left")
                label(row, f"CPU {cpu_p:4.1f}%", 9, level_color(cpu_p * 2)).pack(side="left", padx=8)
                label(row, f"MEM {mem_p:4.1f}%", 9, DIM).pack(side="left")

        self.background(collect, show)

    def save_report(self):
        path = filedialog.asksaveasfilename(
            title="Save system report", defaultextension=".txt",
            initialfile=f"jarvis-report-{dt.datetime.now():%Y%m%d-%H%M}.txt",
            filetypes=[("Text file", "*.txt")])
        if path:
            self.run_command(["report", "-o", path], switch=False,
                             done=lambda: messagebox.showinfo("Jarvis", f"Report saved to\n{path}"))

    def check_internet(self):
        def work():
            start = time.time()
            try:
                with socket.create_connection(("1.1.1.1", 53), timeout=3):
                    return f"You're online. Reply in {(time.time() - start) * 1000:.0f} ms."
            except OSError:
                return "I can't reach the internet right now."

        self.background(work, lambda msg: messagebox.showinfo("Internet", msg))

    # ---- Processes -------------------------------------------------------- #

    def build_processes(self, page):
        self.header(page, "Processes", "Everything running on your computer. Select one to end it.")
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 8))
        label(bar, "Search", 10, DIM).pack(side="left")
        self.proc_search = tk.StringVar()
        e = tk.Entry(bar, textvariable=self.proc_search, bg=PANEL_2, fg=TEXT, insertbackground=TEXT,
                     relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT, font=(FONT, 10), width=26)
        e.pack(side="left", padx=8, ipady=5)
        self.proc_search.trace_add("write", lambda *_: self.refresh_processes())
        label(bar, "Sort by", 10, DIM).pack(side="left", padx=(10, 0))
        self.proc_sort = ttk.Combobox(bar, values=["CPU", "Memory", "Name"], state="readonly", width=9)
        self.proc_sort.set("CPU")
        self.proc_sort.pack(side="left", padx=8)
        self.proc_sort.bind("<<ComboboxSelected>>", lambda _: self.refresh_processes())
        button(bar, "Force quit", lambda: self.end_process(True), "danger").pack(side="right")
        button(bar, "End process", lambda: self.end_process(False), "danger").pack(side="right", padx=8)
        button(bar, "Refresh", self.refresh_processes).pack(side="right")

        box = panel(page)
        box.pack(fill="both", expand=True)
        self.proc_tree = make_table(box, [("PID", 80), ("Name", 230), ("User", 130), ("CPU %", 80),
                                          ("Memory %", 90), ("Memory", 100), ("Status", 100)], height=20)
        self.proc_count = label(page, "", 9, DIM, bg=BG)
        self.proc_count.pack(anchor="w", pady=(6, 0))
        self._proc_auto()

    def _proc_auto(self):
        if getattr(self, "current", "") == "Processes" and not self.proc_tree.selection():
            self.refresh_processes()
        self.after(4000, self._proc_auto)

    def refresh_processes(self):
        self._proc_gen = getattr(self, "_proc_gen", 0) + 1
        gen = self._proc_gen  # ignore results from older, slower refreshes
        query = self.proc_search.get().lower().strip()
        sort = self.proc_sort.get()

        def collect():
            ncpu = psutil.cpu_count() or 1
            rows = []
            for p, i in jarvis.processes(["pid", "name", "username", "memory_info", "memory_percent", "status"]):
                try:
                    name = i["name"] or "?"
                    if query and query not in name.lower() and query != str(i["pid"]):
                        continue
                    rows.append([i["pid"], name, (i["username"] or "").split("\\")[-1],
                                 round(p.cpu_percent(None) / ncpu, 1), round(i["memory_percent"] or 0, 1),
                                 i["memory_info"].rss if i["memory_info"] else 0, i["status"]])
                except psutil.Error:
                    pass
            if sort == "Name":
                rows.sort(key=lambda r: r[1].lower())
            else:
                rows.sort(key=lambda r: (r[3], r[5]) if sort == "CPU" else r[5], reverse=True)
            return rows

        def show(rows):
            if gen != self._proc_gen:
                return
            selected = self.proc_tree.selection()
            keep = {self.proc_tree.item(s, "values")[0] for s in selected}
            fill_table(self.proc_tree, [r[:5] + [jarvis.human(r[5]), r[6]] for r in rows])
            for item in self.proc_tree.get_children():
                if self.proc_tree.item(item, "values")[0] in keep:
                    self.proc_tree.selection_add(item)
            self.proc_count.configure(text=f"{len(rows)} processes shown")

        self.background(collect, show)

    def end_process(self, force: bool):
        sel = self.proc_tree.selection()
        if not sel:
            return messagebox.showinfo("Jarvis", "Select a process in the list first.")
        targets = [(int(self.proc_tree.item(s, "values")[0]), self.proc_tree.item(s, "values")[1]) for s in sel]
        names = "\n".join(f"  {n} (PID {pid})" for pid, n in targets[:10])
        verb = "Force quit" if force else "End"
        if not messagebox.askyesno("Jarvis", f"{verb} these processes?\n\n{names}\n\nUnsaved work in them will be lost."):
            return
        errors = []
        for pid, name in targets:
            if pid == os.getpid():
                continue
            try:
                p = psutil.Process(pid)
                p.kill() if force else p.terminate()
            except psutil.AccessDenied:
                errors.append(f"{name}: permission denied (run Jarvis as administrator)")
            except psutil.NoSuchProcess:
                pass
        if errors:
            messagebox.showwarning("Jarvis", "\n".join(errors))
        self.proc_tree.selection_remove(*self.proc_tree.selection())
        self.after(800, self.refresh_processes)

    # ---- Network ---------------------------------------------------------- #

    def build_network(self, page):
        self.header(page, "Network", "Connections, addresses and the ports programs are listening on.")
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 8))
        button(bar, "Refresh", self.refresh_network).pack(side="left")
        button(bar, "Check internet", self.check_internet).pack(side="left", padx=8)
        label(bar, "Ping", 10, DIM).pack(side="left", padx=(16, 0))
        self.ping_host = tk.StringVar(value="google.com")
        tk.Entry(bar, textvariable=self.ping_host, bg=PANEL_2, fg=TEXT, insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT, font=(FONT, 10), width=22).pack(side="left", padx=8, ipady=5)
        button(bar, "Go", lambda: self.run_command(["ping", self.ping_host.get().strip() or "google.com"]),
               "accent").pack(side="left")

        top = panel(page)
        top.pack(fill="x", pady=(0, 10))
        label(top, "INTERFACES", 9, DIM, True).pack(anchor="w", padx=12, pady=(10, 4))
        self.nic_tree = make_table(top, [("Interface", 160), ("State", 70), ("Addresses", 380),
                                         ("Sent", 100), ("Received", 100)], height=5)
        bottom = panel(page)
        bottom.pack(fill="both", expand=True)
        label(bottom, "LISTENING PORTS  (exposed = reachable from other machines)", 9, DIM, True).pack(
            anchor="w", padx=12, pady=(10, 4))
        self.port_tree = make_table(bottom, [("Port", 80), ("Protocol", 80), ("Address", 160),
                                             ("PID", 80), ("Program", 200), ("Scope", 100)], height=10)

    def refresh_network(self):
        def collect():
            stats, io = psutil.net_if_stats(), psutil.net_io_counters(pernic=True)
            nics = []
            for nic, addrs in psutil.net_if_addrs().items():
                ips = [a.address.split("%")[0] for a in addrs if a.family in (socket.AF_INET, socket.AF_INET6)]
                st, n = stats.get(nic), io.get(nic)
                nics.append([nic, "up" if st and st.isup else "down", ", ".join(ips) or "-",
                             jarvis.human(n.bytes_sent) if n else "-", jarvis.human(n.bytes_recv) if n else "-"])
            ports, seen, note = [], set(), ""
            try:
                conns = psutil.net_connections(kind="inet")
            except psutil.AccessDenied:
                conns, note = [], "admin"
            for cn in conns:
                if cn.status != psutil.CONN_LISTEN and not (cn.type == socket.SOCK_DGRAM and not cn.raddr):
                    continue
                key = (cn.laddr.ip, cn.laddr.port, cn.type)
                if key in seen:
                    continue
                seen.add(key)
                name = "?"
                if cn.pid:
                    with contextlib.suppress(psutil.Error):
                        name = psutil.Process(cn.pid).name()
                exposed = cn.laddr.ip not in ("127.0.0.1", "::1", "localhost")
                ports.append([cn.laddr.port, "TCP" if cn.type == socket.SOCK_STREAM else "UDP", cn.laddr.ip,
                              cn.pid or "-", name, "exposed" if exposed else "local only"])
            ports.sort(key=lambda r: r[0])
            return nics, ports, note

        def show(r):
            nics, ports, note = r
            fill_table(self.nic_tree, nics)
            fill_table(self.port_tree, ports)
            if note:
                messagebox.showinfo("Jarvis", "Listing ports on this system needs administrator rights.\n"
                                              "Restart Jarvis as administrator to see them.")

        self.background(collect, show)

    # ---- Storage ---------------------------------------------------------- #

    def build_storage(self, page):
        self.header(page, "Storage", "Disk space, the biggest files, and old temporary files you can clear.")
        disks = panel(page)
        disks.pack(fill="x", pady=(0, 10))
        label(disks, "DISKS", 9, DIM, True).pack(anchor="w", padx=12, pady=(10, 4))
        self.disk_box = tk.Frame(disks, bg=PANEL)
        self.disk_box.pack(fill="x", padx=12, pady=(0, 10))

        cols = tk.Frame(page, bg=BG)
        cols.pack(fill="both", expand=True)

        big = panel(cols)
        big.pack(side="left", fill="both", expand=True, padx=(0, 6))
        hdr = tk.Frame(big, bg=PANEL)
        hdr.pack(fill="x", padx=12, pady=(10, 6))
        label(hdr, "BIGGEST FILES", 9, DIM, True).pack(side="left")
        button(hdr, "Choose folder…", self.scan_big_files, "accent").pack(side="right")
        self.big_status = label(big, "Pick a folder to find what's taking up space.", 9, DIM)
        self.big_status.pack(anchor="w", padx=12)
        self.big_tree = make_table(big, [("Size", 90), ("File", 420)], height=10)
        self.big_tree.bind("<Double-1>", self.open_containing_folder)

        clean = panel(cols)
        clean.pack(side="left", fill="both", padx=(6, 0))
        clean.configure(width=320)
        label(clean, "CLEAN TEMPORARY FILES", 9, DIM, True).pack(anchor="w", padx=14, pady=(10, 6))
        label(clean, "Removes temp and cache files that haven't\nbeen touched for a while.", 10, TEXT,
              justify="left").pack(anchor="w", padx=14)
        r = tk.Frame(clean, bg=PANEL)
        r.pack(anchor="w", padx=14, pady=10)
        label(r, "Older than", 10, DIM).pack(side="left")
        self.clean_days = ttk.Combobox(r, values=["1", "3", "7", "14", "30"], width=4, state="readonly")
        self.clean_days.set("7")
        self.clean_days.pack(side="left", padx=6)
        label(r, "days", 10, DIM).pack(side="left")
        button(clean, "Scan", self.scan_clean).pack(anchor="w", padx=14)
        self.clean_result = label(clean, "", 10, ACCENT, justify="left", wraplength=280)
        self.clean_result.pack(anchor="w", padx=14, pady=10)
        self.clean_btn = button(clean, "Delete them", self.do_clean, "danger")
        self._clean_files: list[str] = []

    def refresh_disks(self):
        for w in self.disk_box.winfo_children():
            w.destroy()
        for part in jarvis._disks():
            try:
                du = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            row = tk.Frame(self.disk_box, bg=PANEL)
            row.pack(fill="x", pady=3)
            label(row, part.mountpoint[:22], 10, TEXT, width=18, anchor="w").pack(side="left")
            c = tk.Canvas(row, height=10, bg=PANEL_2, highlightthickness=0, width=360)
            c.pack(side="left", padx=8)
            c.create_rectangle(0, 0, 360 * du.percent / 100, 10, fill=level_color(du.percent), width=0)
            label(row, f"{du.percent:.0f}%   {jarvis.human(du.free)} free of {jarvis.human(du.total)}",
                  9, DIM).pack(side="left")

    def scan_big_files(self):
        folder = filedialog.askdirectory(title="Find big files in…", initialdir=str(Path.home()))
        if not folder:
            return
        self.big_status.configure(text=f"Scanning {folder}… this can take a minute.")
        fill_table(self.big_tree, [])

        def work():
            found, count = [], 0
            for dirpath, dirnames, filenames in os.walk(folder, onerror=lambda e: None):
                dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    with contextlib.suppress(OSError):
                        if not os.path.islink(fp):
                            found.append((os.path.getsize(fp), fp))
                            count += 1
                if len(found) > 5000:
                    found = sorted(found, reverse=True)[:100]
            return sorted(found, reverse=True)[:100], count

        def show(r):
            found, count = r
            fill_table(self.big_tree, [[jarvis.human(s), p] for s, p in found])
            self.big_status.configure(text=f"Scanned {count:,} files in {folder}. Double-click to open its folder.")

        self.background(work, show)

    def open_containing_folder(self, _event):
        sel = self.big_tree.selection()
        if not sel:
            return
        folder = os.path.dirname(self.big_tree.item(sel[0], "values")[1])
        import subprocess
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])

    def scan_clean(self):
        days = int(self.clean_days.get())
        self.clean_result.configure(text="Scanning…")
        self.clean_btn.pack_forget()

        def work():
            cutoff = time.time() - days * 86400
            files, total = [], 0
            for d in jarvis._temp_dirs():
                for dirpath, _, filenames in os.walk(d, onerror=lambda e: None):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        with contextlib.suppress(OSError):
                            st = os.lstat(fp)
                            if st.st_mtime < cutoff and os.access(fp, os.W_OK):
                                files.append(fp)
                                total += st.st_size
            return files, total

        def show(r):
            self._clean_files, total = r
            if not self._clean_files:
                self.clean_result.configure(text="Nothing to clean. 👍")
                return
            dirs = ", ".join(str(d) for d in jarvis._temp_dirs())
            self.clean_result.configure(
                text=f"Found {len(self._clean_files):,} files, {jarvis.human(total)} in total.\n\nIn: {dirs}")
            self.clean_btn.pack(anchor="w", padx=14, pady=(0, 12))

        self.background(work, show)

    def do_clean(self):
        files = self._clean_files
        if not files or not messagebox.askyesno("Jarvis", f"Delete {len(files):,} old temporary files?"):
            return

        def work():
            freed = 0
            for fp in files:
                with contextlib.suppress(OSError):
                    size = os.lstat(fp).st_size
                    os.remove(fp)
                    freed += size
            return freed

        def show(freed):
            self.clean_btn.pack_forget()
            self._clean_files = []
            self.clean_result.configure(text=f"Done. Freed {jarvis.human(freed)}.")
            self.refresh_disks()

        self.background(work, show)

    # ---- Services --------------------------------------------------------- #

    def build_services(self, page):
        self.header(page, "Services", "Background services. Starting or stopping them needs administrator rights.")
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 8))
        button(bar, "Show running services", lambda: self.run_command(["services"], target=self.svc_out),
               "accent").pack(side="left")
        button(bar, "Show all", lambda: self.run_command(["services", "--all"], target=self.svc_out)).pack(
            side="left", padx=8)
        label(bar, "Service name", 10, DIM).pack(side="left", padx=(20, 0))
        self.svc_name = tk.StringVar()
        tk.Entry(bar, textvariable=self.svc_name, bg=PANEL_2, fg=TEXT, insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT, font=(FONT, 10), width=20).pack(side="left", padx=8, ipady=5)
        for action in ("start", "stop", "restart"):
            button(bar, action.capitalize(), lambda a=action: self.service(a),
                   "danger" if action == "stop" else "normal").pack(side="left", padx=2)
        box = panel(page)
        box.pack(fill="both", expand=True)
        self.svc_out = self.make_console(box)

    def service(self, action):
        name = self.svc_name.get().strip()
        if not name:
            return messagebox.showinfo("Jarvis", "Type the service name first (you can copy it from the list).")
        self.run_command(["service", action, name], target=self.svc_out)

    # ---- Ask Jarvis console ---------------------------------------------- #

    def build_console(self, page):
        self.header(page, "Ask Jarvis", 'Type a request in plain English, e.g. "what\'s eating my ram" or "open ports".')
        chips = tk.Frame(page, bg=BG)
        chips.pack(fill="x", pady=(0, 8))
        for text in ("How's my system?", "What's using memory?", "Open ports", "Is chrome running",
                     "Who is logged in", "Help"):
            b = button(chips, text, lambda t=text: self.ask(t))
            b.configure(font=(FONT, 9), padx=10, pady=4)
            b.pack(side="left", padx=(0, 6))
        box = panel(page)
        box.pack(fill="both", expand=True)
        self.console = self.make_console(box)

        entry_row = tk.Frame(page, bg=BG)
        entry_row.pack(fill="x", pady=(10, 0))
        self.ask_var = tk.StringVar()
        e = tk.Entry(entry_row, textvariable=self.ask_var, bg=PANEL_2, fg=TEXT, insertbackground=ACCENT,
                     relief="flat", highlightthickness=1, highlightbackground=LINE,
                     highlightcolor=ACCENT, font=(FONT, 12))
        e.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))
        e.bind("<Return>", lambda _: self.ask(self.ask_var.get()))
        button(entry_row, "Send", lambda: self.ask(self.ask_var.get()), "accent").pack(side="left")
        self.console_target = self.console

    def make_console(self, master) -> tk.Text:
        frame = tk.Frame(master, bg=PANEL)
        frame.pack(fill="both", expand=True, padx=2, pady=2)
        t = tk.Text(frame, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", font=(MONO, 10),
                    highlightthickness=0, bd=0,
                    wrap="none", padx=12, pady=10, state="disabled")
        sy = ttk.Scrollbar(frame, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        for tag, col in (("you", GREEN), ("jarvis", ACCENT), ("dim", DIM), ("err", RED)):
            t.tag_configure(tag, foreground=col)
        return t

    def console_write(self, s: str, color: str | None = None) -> None:
        t = self.console_target
        t.configure(state="normal")
        tag = {GREEN: "you", ACCENT: "jarvis", DIM: "dim", RED: "err"}.get(color or "")
        if tag is None and s.startswith("Jarvis:"):
            tag = "jarvis"
        t.insert("end", s, tag or ())
        t.see("end")
        t.configure(state="disabled")

    def ask(self, text: str, stay: bool = False):
        text = text.strip()
        if not text:
            return
        self.ask_var.set("")
        if not stay:
            self.show("Ask Jarvis")
        self.console_target = self.console
        self.console_write(f"\nyou › {text}\n", GREEN)
        self.hud_heard.configure(text=f"You:  {text}")
        low = text.lower().strip(" ?!.")
        if low in ("clear", "cls"):
            self.console.configure(state="normal")
            self.console.delete("1.0", "end")
            self.console.configure(state="disabled")
            return
        if low in ("help", "commands"):
            self.console_write(jarvis.HELP)
        self._handle_async(text, via_voice=False)

    def _handle_async(self, text: str, via_voice: bool):
        def work():
            self._via_voice = via_voice
            self.ui(lambda: self.set_state("thinking"))
            try:
                self.jarvis.handle(text)
            except Exception as e:
                self.ctx.say(f"I'm afraid something went wrong, {self.address}: {e}")
            finally:
                self.ui(lambda: self.set_state("idle") if self.state == "thinking" else None)

        threading.Thread(target=work, daemon=True).start()

    def run_command(self, argv: list[str], target: tk.Text | None = None, switch: bool = True, done=None):
        """Run a jarvis.py command in the background, streaming its output to a console."""
        if not self._busy.acquire(blocking=False):
            return messagebox.showinfo("Jarvis", "Still working on the last request, one moment…")
        if target is None and switch and self.current != "Ask Jarvis":
            self.show("Ask Jarvis")
            self.console_write(f"\n› {' '.join(argv)}\n", GREEN)
        self.console_target = target or self.console
        if target is not None:
            target.configure(state="normal")
            target.delete("1.0", "end")
            target.configure(state="disabled")
            self.console_write("Working…\n", DIM)

        def work():
            try:
                args = jarvis.build_parser().parse_args(argv)
                with contextlib.redirect_stdout(QueueWriter(self)):
                    args.func(args)
            except SystemExit:
                self.ui(lambda: self.console_write("That command needs more details. Type 'help'.\n", RED))
            except Exception as e:
                self.ui(lambda e=e: self.console_write(f"Something went wrong: {e}\n", RED))
            finally:
                self._busy.release()
                if done:
                    self.ui(done)

        threading.Thread(target=work, daemon=True).start()


def main():
    set_dpi_awareness()
    app = JarvisApp()
    app.mainloop()


if __name__ == "__main__":
    main()
