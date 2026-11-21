"""
Jarvis's personality: turns a spoken or typed request into an action and a
short, polite spoken reply.

The interface (GUI or terminal) passes in a Context that knows how to speak,
ask for confirmation, show a page and print command output.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable

import psutil

import jarvis


class Context:
    """What the assistant can do with its surroundings. Override in the interface."""

    address = "sir"

    def say(self, text: str) -> None:
        print(f"Jarvis: {text}")

    def confirm(self, question: str) -> bool:
        self.say(question)
        return input("[y/N] ").strip().lower() in ("y", "yes")

    def show(self, page: str) -> None:
        pass

    def output(self, argv: list[str]) -> None:
        """Print the full text output of a jarvis.py command (tables etc.)."""
        args = jarvis.build_parser().parse_args(argv)
        args.func(args)

    def stop_speaking(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Phrasing helpers
# --------------------------------------------------------------------------- #

UNITS = {"B": "bytes", "KB": "kilobytes", "MB": "megabytes", "GB": "gigabytes", "TB": "terabytes"}
PRETTY = {"msedge": "Edge", "code": "VS Code", "explorer": "File Explorer", "winword": "Word",
          "powerpnt": "PowerPoint", "excel": "Excel", "teams": "Teams", "ms-teams": "Teams",
          "chrome": "Chrome", "firefox": "Firefox", "python": "Python", "python3": "Python",
          "svchost": "Windows services", "dwm": "the window manager", "msmpeng": "Windows Defender",
          "windowserver": "the window server", "kernel_task": "the system kernel"}


def spoken_size(n: float) -> str:
    num, unit = jarvis.human(n).split()
    num = str(round(float(num))) if unit in ("B", "KB", "MB") else num.removesuffix(".0")
    return f"{num} {UNITS.get(unit, unit)}"


def pretty(name: str) -> str:
    base = re.sub(r"\.(exe|app|bin)$", "", name, flags=re.I)
    return PRETTY.get(base.lower(), base[:1].upper() + base[1:])


def listing(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def time_of_day() -> str:
    h = dt.datetime.now().hour
    return "morning" if h < 12 else "afternoon" if h < 18 else "evening"


def greeting(address: str) -> str:
    return random.choice([
        f"Good {time_of_day()}, {address}. Jarvis online and at your service.",
        f"Good {time_of_day()}, {address}. All systems are standing by.",
        f"Good {time_of_day()}, {address}. What can I do for you?",
    ])


def _grouped_processes(sample: float = 0.6) -> list[dict]:
    """CPU and memory per program, adding up all of its processes."""
    procs = jarvis.processes(["name", "memory_info"])
    for p, _ in procs:
        with contextlib.suppress(psutil.Error):
            p.cpu_percent(None)
    time.sleep(sample)
    ncpu = psutil.cpu_count() or 1
    groups: dict[str, dict] = {}
    me = os.getpid()
    for p, info in procs:
        try:
            if p.pid in (0, me):
                continue
            name = info["name"] or "?"
            if name.lower() in ("system idle process", "idle"):
                continue
            g = groups.setdefault(pretty(name), {"name": pretty(name), "cpu": 0.0, "mem": 0, "count": 0, "raw": name})
            g["cpu"] += p.cpu_percent(None) / ncpu
            g["mem"] += info["memory_info"].rss if info["memory_info"] else 0
            g["count"] += 1
        except psutil.Error:
            pass
    return list(groups.values())


def _local_ip() -> str | None:
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # picks the outgoing interface; sends nothing
            return s.getsockname()[0]
    return None


def _online(timeout: float = 2.5) -> float | None:
    start = time.time()
    with contextlib.suppress(OSError):
        with socket.create_connection(("1.1.1.1", 53), timeout=timeout):
            return (time.time() - start) * 1000
    return None


# --------------------------------------------------------------------------- #
# The assistant
# --------------------------------------------------------------------------- #

class Assistant:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    @property
    def sir(self) -> str:
        return self.ctx.address

    def say(self, text: str) -> None:
        self.ctx.say(text)

    # Each rule: (pattern, handler). Checked before the system commands.
    def _rules(self) -> list[tuple[str, Callable[[re.Match], None]]]:
        return [
            (r"^(stop|cancel|never ?mind|quiet|silence|shut up)$", lambda m: self.ctx.stop_speaking()),
            (r"^(hello|hi|hey|good (morning|afternoon|evening)|you there|are you there|wake up)$", self.hello),
            (r"\b(thank(s| you)|cheers|nice work|well done|good job)\b", self.thanks),
            (r"\b(who|what) are you\b|\byour name\b|\bintroduce yourself\b", self.who),
            (r"^(help|what can you do|what can i (say|ask))", self.help),
            (r"\bhow are you\b", lambda m: self.say(random.choice([
                f"Running at full capacity, {self.sir}. Thank you for asking.",
                f"Quite well, {self.sir}. Your computer, on the other hand, I'm keeping an eye on.",
            ]))),
            (r"\b(what(?:'s| is) the time|what time is it|tell me the time)\b", self.time),
            (r"\b(what(?:'s| is) (the|today's) date|what day is it|what is today)\b", self.date),
            (r"^(goodbye|bye|good night|that(?:'s| is) all|go to sleep|sleep now|dismissed)$", self.goodbye),
            (r"\b(shut ?down|power off|turn off) (the |my )?(computer|pc|laptop|system|machine)\b",
             lambda m: self.power("shutdown")),
            (r"\b(restart|reboot) (the |my )?(computer|pc|laptop|system|machine)\b", lambda m: self.power("restart")),
            (r"\block (the |my )?(computer|pc|laptop|screen|system|machine)\b", lambda m: self.power("lock")),
            (r"\b(put (the |my )?(computer|pc|laptop) to sleep|sleep mode)\b", lambda m: self.power("sleep")),
            (r"^(?:search (?:the web |online |google )?for|google|look up|search the web for) (?P<q>.+)$", self.web_search),
            (r"^(?:open|launch|start) (?!(?:the |my )?(?:ports?|service|listening)\b)(?:up )?(?:the )?(?P<what>[\w .\-]+?)(?: app| application| website)?$",
             self.open),
            (r"\b(how much|what(?:'s| is)) (free )?(disk|storage|space)\b|\bdisk space\b", self.disk),
            (r"\bbattery\b", self.battery),
            (r"\b(am i|are we|is (this|the) (computer|pc))\s+(online|connected)\b|\binternet\b",
             lambda m: self.do_net(["net"])),
            (r"\b(cpu|processor)\b", lambda m: self.top("cpu")),
        ]

    def handle(self, text: str) -> None:
        t = text.strip()
        t = re.sub(r"^(ok(ay)?|hey|hi)?\s*jarvis[\s,.!]*", "", t, flags=re.I)
        t = re.sub(r"^(please|could you|can you|would you)\s+", "", t, flags=re.I)
        t = re.sub(r"\s+please$", "", t, flags=re.I).strip(" ?!.,")
        if not t:
            return self.say(f"Yes, {self.sir}?")
        low = t.lower()
        for pattern, fn in self._rules():
            m = re.search(pattern, low)
            if m:
                if fn.__name__ in ("web_search", "open"):  # keep original capitalisation for queries
                    m = re.search(pattern, t, re.I)
                return fn(m)
        argv = None
        with contextlib.suppress(ValueError):
            argv = jarvis.interpret(t)
        if not argv:
            return self.say(random.choice([
                f"I'm sorry, {self.sir}, I didn't quite follow. You can ask me about your system's health, "
                "programs that are running, the network, or storage.",
                f"I'm afraid that's beyond my remit, {self.sir}. Try asking how the system is doing.",
            ]))
        handler = getattr(self, "do_" + argv[0], None)
        if handler:
            handler(argv)
        else:
            self.ctx.output(argv)

    # ---- conversation ---------------------------------------------------- #

    def hello(self, m):
        self.say(random.choice([f"At your service, {self.sir}.", f"Hello, {self.sir}. How can I help?",
                                f"I'm here, {self.sir}."]))

    def thanks(self, m):
        self.say(random.choice([f"You're welcome, {self.sir}.", f"Always a pleasure, {self.sir}.",
                                f"Happy to help, {self.sir}."]))

    def who(self, m):
        self.say(f"I'm Jarvis, {self.sir}. I look after this computer: its health, its programs, its network "
                 "and its storage. Just ask.")

    def help(self, m):
        self.say(f"Certainly, {self.sir}. You can ask how the system is doing, what's using the processor or "
                 "memory, whether a program is running, or ask me to close one. I can check the network and "
                 "open ports, find large files, clear temporary files, open apps and websites, search the web, "
                 "and lock, restart or shut down the computer.")

    def time(self, m):
        now = dt.datetime.now()
        self.say(f"It's {now:%I:%M}".replace(" 0", " ") + f" {'in the morning' if now.hour < 12 else 'in the afternoon' if now.hour < 18 else 'in the evening'}, {self.sir}.")

    def date(self, m):
        now = dt.datetime.now()
        self.say(f"Today is {now:%A}, the {now.day}{self._ordinal(now.day)} of {now:%B}, {self.sir}.")

    @staticmethod
    def _ordinal(n: int) -> str:
        return "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

    def goodbye(self, m):
        self.say(random.choice([f"Very good, {self.sir}. I'll be here if you need me.",
                                f"Standing by, {self.sir}."]))

    # ---- system health --------------------------------------------------- #

    def do_status(self, argv=None):
        cpu = psutil.cpu_percent(interval=0.6)
        vm = psutil.virtual_memory()
        root = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
        du = psutil.disk_usage(root)
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        concerns = []
        if cpu > 85:
            concerns.append(f"the processor is working hard at {cpu:.0f} percent")
        if vm.percent > 85:
            concerns.append(f"memory is {vm.percent:.0f} percent full")
        for part in jarvis._disks():
            with contextlib.suppress(OSError):
                pct = psutil.disk_usage(part.mountpoint).percent
                if pct > 90:
                    concerns.append(f"the drive at {part.mountpoint} is {pct:.0f} percent full")
        if bat and not bat.power_plugged and bat.percent < 20:
            concerns.append(f"the battery is down to {bat.percent:.0f} percent")
        figures = (f"Processor at {cpu:.0f} percent, memory at {vm.percent:.0f} percent, "
                   f"and the main drive is {du.percent:.0f} percent full")
        if bat:
            figures += f". Battery at {bat.percent:.0f} percent{', charging' if bat.power_plugged else ''}"
        if concerns:
            self.say(f"A few things need your attention, {self.sir}: {listing(concerns)}. {figures}.")
        else:
            opener = random.choice([f"All systems are running normally, {self.sir}.",
                                    f"Everything looks in order, {self.sir}.",
                                    f"All systems nominal, {self.sir}."])
            self.say(f"{opener} {figures}.")

    def disk(self, m=None):
        parts = []
        for part in jarvis._disks():
            with contextlib.suppress(OSError):
                du = psutil.disk_usage(part.mountpoint)
                if du.total > 2 * 1024 ** 3:
                    parts.append(f"{spoken_size(du.free)} free on {part.mountpoint.rstrip(os.sep) or 'the main drive'}")
        self.say(f"You have {listing(parts[:3]) or 'very little space'}, {self.sir}.")

    def battery(self, m=None):
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if not bat:
            return self.say(f"This machine doesn't appear to have a battery, {self.sir}. Mains power only.")
        if bat.power_plugged:
            return self.say(f"Battery at {bat.percent:.0f} percent and charging, {self.sir}.")
        mins = bat.secsleft // 60 if bat.secsleft and bat.secsleft > 0 else None
        left = f", roughly {mins // 60} hours and {mins % 60} minutes remaining" if mins else ""
        self.say(f"Battery at {bat.percent:.0f} percent{left}, {self.sir}.")

    def top(self, kind: str):
        self.do_top(["top", kind])

    def do_top(self, argv):
        kind = argv[1] if len(argv) > 1 else "cpu"
        groups = _grouped_processes()
        self.ctx.output(["top", kind, "-n", "10"])
        if kind == "mem":
            groups.sort(key=lambda g: g["mem"], reverse=True)
            g = groups[0]
            others = [x["name"] for x in groups[1:3]]
            self.say(f"{g['name']} is using the most memory, {self.sir}, about {spoken_size(g['mem'])}"
                     + (f", followed by {listing(others)}." if others else "."))
        else:
            groups.sort(key=lambda g: g["cpu"], reverse=True)
            g = groups[0]
            if g["cpu"] < 3:
                return self.say(f"Nothing is working the processor particularly hard, {self.sir}. "
                                f"Total usage is {psutil.cpu_percent():.0f} percent.")
            others = [x["name"] for x in groups[1:3] if x["cpu"] >= 1]
            self.say(f"{g['name']} is the most demanding right now, {self.sir}, at {g['cpu']:.0f} percent "
                     "of the processor" + (f", followed by {listing(others)}." if others else "."))

    # ---- programs -------------------------------------------------------- #

    def _matching(self, target: str) -> list[psutil.Process]:
        t = target.lower().removesuffix(".exe")
        if t.isdigit():
            with contextlib.suppress(psutil.Error):
                return [psutil.Process(int(t))]
            return []
        aliases = {k for k, v in PRETTY.items() if v.lower() == t} | {t}
        out = []
        for p, info in jarvis.processes(["name"]):
            name = (info["name"] or "").lower().removesuffix(".exe")
            if p.pid in (os.getpid(), os.getppid()):
                continue
            if name in aliases or (len(t) >= 4 and name.startswith(t)):
                out.append(p)
        return out

    def do_find(self, argv):
        procs = self._matching(argv[1])
        if not procs:
            return self.say(f"No, {self.sir}, I don't see anything called {argv[1]} running.")
        mem = sum(p.memory_info().rss for p in procs if p.is_running())
        n = len(procs)
        self.say(f"Yes, {self.sir}. {pretty(procs[0].name())} is running"
                 + (f", {n} processes" if n > 1 else "") + f", using {spoken_size(mem)} of memory.")

    def do_kill(self, argv):
        target = argv[1]
        procs = self._matching(target)
        if not procs:
            return self.say(f"I can't find anything called {target} running, {self.sir}.")
        name = pretty(procs[0].name())
        extra = f" That's {len(procs)} processes." if len(procs) > 1 else ""
        if not self.ctx.confirm(f"Shall I close {name}, {self.sir}?{extra} Any unsaved work will be lost."):
            return self.say(f"Very well, {self.sir}. I'll leave it running.")
        denied = 0
        for p in procs:
            try:
                p.terminate()
            except psutil.AccessDenied:
                denied += 1
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(procs, timeout=4)
        for p in alive:
            with contextlib.suppress(psutil.Error):
                p.kill()
        if denied == len(procs):
            return self.say(f"I'm afraid I don't have permission to close {name}, {self.sir}. "
                            "You'd need to run me as administrator.")
        self.say(f"Done, {self.sir}. {name} has been closed.")

    def open(self, m):
        what = m["what"].strip().rstrip(".")
        sites = {"youtube": "https://www.youtube.com", "google": "https://www.google.com",
                 "gmail": "https://mail.google.com", "github": "https://github.com",
                 "linkedin": "https://www.linkedin.com", "netflix": "https://www.netflix.com",
                 "spotify": "https://open.spotify.com", "stack overflow": "https://stackoverflow.com",
                 "claude": "https://claude.ai", "outlook web": "https://outlook.office.com",
                 "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
                 "news": "https://news.google.com", "amazon": "https://www.amazon.com"}
        key = what.lower()
        if key in sites or re.fullmatch(r"[\w\-]+(\.[\w\-]+)+", key):
            url = sites.get(key) or "https://" + key
            webbrowser.open(url)
            return self.say(f"Opening {what}, {self.sir}.")
        if self._launch(key):
            self.say(random.choice([f"Opening {what}, {self.sir}.", f"Right away, {self.sir}.",
                                    f"{what[:1].upper() + what[1:]} is on its way, {self.sir}."]))
        else:
            self.say(f"I couldn't find an app called {what}, {self.sir}.")

    @staticmethod
    def _launch(name: str) -> bool:
        name = re.sub(r"[^\w .\-]", "", name)
        if sys.platform == "win32":
            win = {"notepad": "notepad", "calculator": "calc", "file explorer": "explorer", "explorer": "explorer",
                   "files": "explorer", "task manager": "taskmgr", "settings": "ms-settings:",
                   "control panel": "control", "command prompt": "cmd", "terminal": "wt", "powershell": "powershell",
                   "paint": "mspaint", "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
                   "outlook": "outlook", "edge": "msedge", "chrome": "chrome", "firefox": "firefox",
                   "vs code": "code", "visual studio code": "code", "teams": "ms-teams:", "spotify": "spotify:",
                   "camera": "microsoft.windows.camera:", "snipping tool": "snippingtool"}
            target = win.get(name, name)
            try:
                if target.endswith(":"):
                    os.startfile(target)  # type: ignore[attr-defined]
                else:
                    r = subprocess.run(["cmd", "/c", "start", "", target], capture_output=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    return r.returncode == 0
                return True
            except OSError:
                return False
        if sys.platform == "darwin":
            mac = {"calculator": "Calculator", "notes": "Notes", "finder": "Finder", "files": "Finder",
                   "settings": "System Settings", "terminal": "Terminal", "safari": "Safari",
                   "chrome": "Google Chrome", "vs code": "Visual Studio Code", "activity monitor": "Activity Monitor",
                   "word": "Microsoft Word", "excel": "Microsoft Excel", "mail": "Mail", "music": "Music"}
            return subprocess.run(["open", "-a", mac.get(name, name.title())], capture_output=True).returncode == 0
        linux = {"calculator": "gnome-calculator", "files": "nautilus", "file explorer": "nautilus",
                 "terminal": "x-terminal-emulator", "settings": "gnome-control-center", "chrome": "google-chrome",
                 "vs code": "code", "text editor": "gedit", "system monitor": "gnome-system-monitor"}
        exe = shutil.which(linux.get(name, name).replace(" ", "-"))
        if not exe:
            return False
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True

    def web_search(self, m):
        q = m["q"].strip()
        webbrowser.open("https://www.google.com/search?q=" + __import__("urllib.parse").parse.quote_plus(q))
        self.say(f"Here's what I found for {q}, {self.sir}.")

    # ---- network --------------------------------------------------------- #

    def do_net(self, argv):
        ms = _online()
        ip = _local_ip()
        if ms is None:
            return self.say(f"I'm afraid we're offline, {self.sir}. I can't reach the internet."
                            + (f" Your local address is {ip}." if ip else ""))
        self.say(f"You're online, {self.sir}, with a response time of {ms:.0f} milliseconds."
                 + (f" Your local address is {ip}." if ip else ""))

    def do_ping(self, argv):
        host = argv[1] if len(argv) > 1 else "google.com"
        start = time.time()
        try:
            with socket.create_connection((host, 443), timeout=4):
                self.say(f"{host} replied in {(time.time() - start) * 1000:.0f} milliseconds, {self.sir}.")
        except OSError:
            self.say(f"I can't reach {host}, {self.sir}.")

    def do_ports(self, argv):
        try:
            conns = psutil.net_connections(kind="inet")
        except psutil.AccessDenied:
            return self.say(f"I need administrator rights to see open ports on this system, {self.sir}.")
        seen, exposed, total = set(), [], 0
        for cn in conns:
            if cn.status != psutil.CONN_LISTEN:
                continue
            key = (cn.laddr.ip, cn.laddr.port)
            if key in seen:
                continue
            seen.add(key)
            total += 1
            if cn.laddr.ip not in ("127.0.0.1", "::1"):
                name = "an unknown program"
                if cn.pid:  # Process(None) would mean Jarvis itself
                    with contextlib.suppress(psutil.Error):
                        name = pretty(psutil.Process(cn.pid).name())
                exposed.append(f"{name} on port {cn.laddr.port}")
        if not total:
            return self.say(f"Nothing is listening for connections, {self.sir}. Quiet as a church mouse.")
        msg = f"There are {total} ports open, {self.sir}."
        if exposed:
            msg += f" {len(exposed)} can be reached from other machines: {listing(exposed[:4])}."
            if len(exposed) > 4:
                msg += " The full list is on screen."
        else:
            msg += " All of them are local to this machine only, which is reassuring."
        self.say(msg)

    # ---- storage --------------------------------------------------------- #

    def do_bigfiles(self, argv):
        self.ctx.show("Storage")
        self.say(f"I've opened the storage panel, {self.sir}. Choose a folder and I'll find what's taking up space.")

    def do_clean(self, argv):
        cutoff = time.time() - 7 * 86400
        files, total = [], 0
        for d in jarvis._temp_dirs():
            for dirpath, _, names in os.walk(d, onerror=lambda e: None):
                for f in names:
                    fp = os.path.join(dirpath, f)
                    with contextlib.suppress(OSError):
                        st = os.lstat(fp)
                        if st.st_mtime < cutoff and os.access(fp, os.W_OK):
                            files.append(fp)
                            total += st.st_size
        if not files or total < 1024 * 1024:
            return self.say(f"Your temporary files are already tidy, {self.sir}. Nothing worth clearing.")
        if not self.ctx.confirm(f"I found {spoken_size(total)} of temporary files over a week old, {self.sir}. "
                                "Shall I clear them?"):
            return self.say(f"Very well, {self.sir}. I'll leave them.")
        freed = 0
        for fp in files:
            with contextlib.suppress(OSError):
                size = os.lstat(fp).st_size
                os.remove(fp)
                freed += size
        self.say(f"Done, {self.sir}. {spoken_size(freed)} freed.")

    # ---- other system commands ------------------------------------------ #

    def do_users(self, argv):
        names = sorted({u.name for u in psutil.users()})
        if not names:
            return self.say(f"No one else is signed in, {self.sir}.")
        if len(names) == 1:
            return self.say(f"Just {names[0]}, {self.sir}.")
        self.say(f"{len(names)} accounts are signed in, {self.sir}: {listing(names)}.")

    def do_services(self, argv):
        self.ctx.show("Services")
        self.ctx.output(argv)
        self.say(f"The list of services is on screen, {self.sir}.")

    def do_service(self, argv):
        action, name = argv[1], argv[2]
        if not self.ctx.confirm(f"Shall I {action} the {name} service, {self.sir}?"):
            return self.say(f"Very well, {self.sir}.")
        self.ctx.output(argv + ["-y"])
        self.say(f"I've asked the system to {action} {name}, {self.sir}.")

    def do_watch(self, argv):
        self.ctx.show("Dashboard")
        self.say(f"Bringing up the live dashboard, {self.sir}.")

    def do_report(self, argv):
        desktop = Path.home() / "Desktop"
        folder = desktop if desktop.is_dir() else Path.home()
        path = folder / f"jarvis-report-{dt.datetime.now():%Y%m%d-%H%M}.txt"
        self.ctx.output(["report", "-o", str(path)])
        self.say(f"I've saved a full system report to your {'desktop' if folder == desktop else 'home folder'}, {self.sir}.")

    def do_run(self, argv):
        self.say(f"For safety, {self.sir}, I'd rather you type shell commands in the console than speak them.")

    def power(self, action: str):
        words = {"shutdown": "shut down the computer", "restart": "restart the computer",
                 "lock": "lock the computer", "sleep": "put the computer to sleep"}
        if action in ("shutdown", "restart") and not self.ctx.confirm(
                f"Are you sure you want me to {words[action]}, {self.sir}? Save your work first."):
            return self.say(f"Standing down, {self.sir}.")
        if sys.platform == "win32":
            cmds = {"shutdown": ["shutdown", "/s", "/t", "15"], "restart": ["shutdown", "/r", "/t", "15"],
                    "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
                    "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]}
        elif sys.platform == "darwin":
            cmds = {"shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
                    "restart": ["osascript", "-e", 'tell app "System Events" to restart'],
                    "lock": ["pmset", "displaysleepnow"], "sleep": ["pmset", "sleepnow"]}
        else:
            cmds = {"shutdown": ["systemctl", "poweroff"], "restart": ["systemctl", "reboot"],
                    "lock": ["loginctl", "lock-session"], "sleep": ["systemctl", "suspend"]}
        farewell = {"shutdown": f"Shutting down, {self.sir}. Goodbye.", "restart": f"Restarting now, {self.sir}.",
                    "lock": f"Locking the computer, {self.sir}.", "sleep": f"Goodnight, {self.sir}."}
        self.say(farewell[action])
        try:
            subprocess.Popen(cmds[action], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.say(f"I'm afraid that didn't work, {self.sir}.")
