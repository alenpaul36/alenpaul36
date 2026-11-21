#!/usr/bin/env python3
"""
J.A.R.V.I.S. - a small assistant for managing your own computer.

Run it with no arguments for an interactive prompt where you can type
plain requests ("how's my system?", "top memory", "kill 1234"), or pass
a command directly:  python jarvis.py status

Works on Linux, macOS and Windows. Needs `psutil`; add --voice to hear
replies in a British male voice. For the full voice assistant with a
microphone, run jarvis_gui.py instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    sys.exit("Jarvis needs psutil. Install it with:  pip install psutil")

# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if os.name == "nt":
    os.system("")  # enables ANSI escape codes in Windows terminals


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


CYAN, GREEN, YELLOW, RED, DIM, BOLD = "36", "32", "33", "31", "2", "1"


class Voice:
    """Prints replies and, with --voice, speaks them in a British male voice (see voice.py)."""

    def __init__(self, enabled: bool):
        self.speaker = None
        if enabled:
            try:
                from voice import Speaker

                self.speaker = Speaker()
            except Exception as e:
                print(c(f"Voice unavailable ({e}). Continuing silently.", YELLOW))

    @property
    def engine(self):
        return self.speaker

    def say(self, text: str) -> None:
        print(c("Jarvis: ", CYAN + ";" + BOLD) + text)
        if self.speaker:
            self.speaker.say(text)


VOICE = Voice(False)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def bar(pct: float, width: int = 24) -> str:
    filled = int(round(pct / 100 * width))
    color = GREEN if pct < 70 else YELLOW if pct < 90 else RED
    return c("█" * filled, color) + c("░" * (width - filled), DIM) + f" {pct:5.1f}%"


def table(rows: list[list], headers: list[str]) -> None:
    rows = [[str(x) for x in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print(c("  ".join(h.ljust(w) for h, w in zip(headers, widths)), BOLD))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def sh(cmd, shell: bool = False) -> int:
    """Run a command and print its output, so it also shows up in the GUI."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no flashing console on Windows
    try:
        r = subprocess.run(cmd, shell=shell, capture_output=True, text=True, errors="replace",
                           stdin=subprocess.DEVNULL, creationflags=flags)
    except FileNotFoundError as e:
        print(c(f"  {e}", RED))
        return 127
    for out in (r.stdout, r.stderr):
        if out.strip():
            print(out.rstrip())
    return r.returncode


def confirm(question: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    try:
        return input(c(f"{question} [y/N] ", YELLOW)).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def uptime() -> str:
    secs = int(time.time() - psutil.boot_time())
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, _ = divmod(secs, 60)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_status(args) -> None:
    """Overview: CPU, memory, disks, battery, uptime."""
    u = platform.uname()
    print(c(f"\n  {u.node}  ·  {u.system} {u.release}  ·  {u.machine}", BOLD))
    print(c(f"  Up {uptime()}  ·  Python {platform.python_version()}  ·  {dt.datetime.now():%Y-%m-%d %H:%M}\n", DIM))

    cpu = psutil.cpu_percent(interval=0.5)
    freq = psutil.cpu_freq()
    freq_s = f" @ {freq.current / 1000:.2f} GHz" if freq and freq.current else ""
    print(f"  CPU     {bar(cpu)}   {psutil.cpu_count()} threads{freq_s}")
    if hasattr(os, "getloadavg"):
        print(c(f"          load {' '.join(f'{x:.2f}' for x in os.getloadavg())}", DIM))

    vm = psutil.virtual_memory()
    print(f"  Memory  {bar(vm.percent)}   {human(vm.used)} / {human(vm.total)}")
    sw = psutil.swap_memory()
    if sw.total:
        print(f"  Swap    {bar(sw.percent)}   {human(sw.used)} / {human(sw.total)}")

    for part in _disks():
        try:
            du = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        print(f"  Disk    {bar(du.percent)}   {human(du.used)} / {human(du.total)}  {c(part.mountpoint, DIM)}")

    bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if bat:
        plug = "charging" if bat.power_plugged else "on battery"
        print(f"  Battery {bar(bat.percent)}   {plug}")

    temps = _temperatures()
    if temps:
        print(c(f"  Temps   {temps}", DIM))
    print()

    warnings = []
    if cpu > 90:
        warnings.append("CPU is running hot")
    if vm.percent > 90:
        warnings.append("memory is nearly full")
    for part in _disks():
        try:
            if psutil.disk_usage(part.mountpoint).percent > 90:
                warnings.append(f"disk {part.mountpoint} is over 90% full")
        except OSError:
            pass
    if bat and not bat.power_plugged and bat.percent < 20:
        warnings.append("battery is low")
    VOICE.say("Heads up: " + "; ".join(warnings) + "." if warnings else "All systems nominal.")


def _disks():
    seen, out = set(), []
    for p in psutil.disk_partitions(all=False):
        if p.fstype in ("squashfs", "tmpfs", "overlay") or "loop" in p.device or p.device in seen:
            continue
        seen.add(p.device)
        out.append(p)
    return out or [type("P", (), {"mountpoint": os.path.abspath(os.sep)})()]


def _temperatures() -> str:
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return ""
    parts = []
    for name, entries in (temps or {}).items():
        if entries:
            parts.append(f"{name} {entries[0].current:.0f}°C")
    return "  ".join(parts[:4])


def processes(attrs: list[str]) -> list[tuple]:
    """(process, info) pairs, with info read immediately.

    psutil reuses Process objects between calls, so `p.info` from process_iter(attrs)
    can be overwritten by another thread (the GUI refreshes several views at once).
    """
    out = []
    for p in psutil.process_iter():
        try:
            out.append((p, p.as_dict(attrs=attrs, ad_value=None)))
        except psutil.Error:
            pass
    return out


def cmd_top(args) -> None:
    """Top processes by CPU or memory."""
    procs = processes(["pid", "name", "username", "memory_info", "memory_percent"])
    for p, _ in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(0.5)
    rows = []
    for p, info in procs:
        try:
            cpu = p.cpu_percent(None)
            rows.append((cpu, info["memory_percent"] or 0, info["pid"], info["name"] or "?",
                         (info["username"] or "").split("\\")[-1], info["memory_info"].rss if info["memory_info"] else 0))
        except psutil.Error:
            continue
    key = 1 if args.sort == "mem" else 0
    rows.sort(key=lambda r: r[key], reverse=True)
    table([[r[2], r[3][:28], r[4][:12], f"{r[0]:.1f}", f"{r[1]:.1f}", human(r[5])] for r in rows[: args.n]],
          ["PID", "NAME", "USER", "CPU%", "MEM%", "RSS"])


def cmd_find(args) -> None:
    """Find processes whose name or command line matches."""
    pat = args.name.lower()
    rows = []
    for p, info in processes(["pid", "name", "cmdline", "status"]):
        if p.pid in (os.getpid(), os.getppid()):
            continue
        try:
            cmd = " ".join(info["cmdline"] or [])
            if pat in (info["name"] or "").lower() or pat in cmd.lower():
                rows.append([info["pid"], info["name"], info["status"], cmd[:70]])
        except psutil.Error:
            continue
    if rows:
        table(rows, ["PID", "NAME", "STATUS", "COMMAND"])
    else:
        VOICE.say(f"I couldn't find any process matching '{args.name}'.")


def cmd_kill(args) -> None:
    """Stop a process by PID or name (asks first)."""
    targets = []
    if args.target.isdigit():
        try:
            targets = [psutil.Process(int(args.target))]
        except psutil.NoSuchProcess:
            return VOICE.say(f"No process with PID {args.target}.")
    else:
        for p, info in processes(["name"]):
            name = (info["name"] or "").lower()
            if args.target.lower() in (name, name.removesuffix(".exe")):
                targets.append(p)
    if not targets:
        return VOICE.say(f"Nothing named '{args.target}' is running.")
    me = os.getpid()
    targets = [p for p in targets if p.pid not in (me, os.getppid())]
    for p in targets:
        try:
            print(f"  {p.pid:>7}  {p.name()}")
        except psutil.Error:
            pass
    verb = "Force-kill" if args.force else "Terminate"
    if not confirm(f"{verb} {len(targets)} process(es)?", args.yes):
        return VOICE.say("Cancelled.")
    for p in targets:
        try:
            p.kill() if args.force else p.terminate()
        except psutil.AccessDenied:
            print(c(f"  Permission denied for PID {p.pid} (try as admin/root).", RED))
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(targets, timeout=3)
    VOICE.say(f"Stopped {len(gone)} process(es)." + (f" {len(alive)} still running; use --force." if alive else ""))


def cmd_net(args) -> None:
    """Network interfaces, addresses and traffic."""
    print(c(f"  Hostname: {socket.gethostname()}", BOLD))
    stats = psutil.net_if_stats()
    io = psutil.net_io_counters(pernic=True)
    rows = []
    for nic, addrs in psutil.net_if_addrs().items():
        ips = [a.address.split("%")[0] for a in addrs if a.family in (socket.AF_INET, socket.AF_INET6)]
        st = stats.get(nic)
        n = io.get(nic)
        rows.append([nic, "up" if st and st.isup else "down", ", ".join(ips)[:48] or "-",
                     human(n.bytes_sent) if n else "-", human(n.bytes_recv) if n else "-"])
    table(rows, ["INTERFACE", "STATE", "ADDRESSES", "SENT", "RECEIVED"])
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2):
            VOICE.say("Internet connection is up.")
    except OSError:
        VOICE.say("I can't reach the internet right now.")


def cmd_ports(args) -> None:
    """Listening ports and the programs that own them."""
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return VOICE.say("I need administrator rights to list ports on this OS. Try running with sudo.")
    rows, seen = [], set()
    for cn in conns:
        if cn.status != psutil.CONN_LISTEN and not (cn.type == socket.SOCK_DGRAM and not cn.raddr):
            continue
        key = (cn.laddr.ip, cn.laddr.port, cn.type)
        if key in seen:
            continue
        seen.add(key)
        name = "?"
        if cn.pid:
            try:
                name = psutil.Process(cn.pid).name()
            except psutil.Error:
                pass
        exposed = cn.laddr.ip not in ("127.0.0.1", "::1", "localhost")
        rows.append([cn.laddr.port, "tcp" if cn.type == socket.SOCK_STREAM else "udp", cn.laddr.ip,
                     cn.pid or "-", name, c("exposed", YELLOW) if exposed else "local"])
    rows.sort(key=lambda r: r[0])
    table(rows, ["PORT", "PROTO", "ADDRESS", "PID", "PROGRAM", "SCOPE"])


def cmd_ping(args) -> None:
    """Check whether a host is reachable."""
    flag = "-n" if os.name == "nt" else "-c"
    if shutil.which("ping"):
        sh(["ping", flag, str(args.count), args.host])
    else:
        start = time.time()
        try:
            socket.create_connection((args.host, 443), timeout=3).close()
            VOICE.say(f"{args.host} answered on port 443 in {(time.time() - start) * 1000:.0f} ms.")
        except OSError as e:
            VOICE.say(f"{args.host} is unreachable: {e}")


def cmd_bigfiles(args) -> None:
    """Largest files under a folder."""
    root = Path(args.path).expanduser()
    found = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if not d.startswith((".git", "node_modules", "proc", "sys"))]
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                if not os.path.islink(fp):
                    found.append((os.path.getsize(fp), fp))
            except OSError:
                pass
    found.sort(reverse=True)
    table([[human(s), p] for s, p in found[: args.n]], ["SIZE", "PATH"])
    VOICE.say(f"Scanned {len(found)} files under {root}.")


def _temp_dirs() -> list[Path]:
    dirs = {Path(tempfile.gettempdir())}
    home = Path.home()
    if sys.platform == "darwin":
        dirs.add(home / "Library" / "Caches")
    elif os.name == "nt":
        if os.environ.get("LOCALAPPDATA"):
            dirs.add(Path(os.environ["LOCALAPPDATA"]) / "Temp")
    else:
        dirs.add(home / ".cache" / "thumbnails")
    return [d for d in dirs if d.exists()]


def cmd_clean(args) -> None:
    """Delete temp/cache files older than N days (dry run unless --apply)."""
    cutoff = time.time() - args.days * 86400
    victims, total = [], 0
    for d in _temp_dirs():
        for dirpath, _, filenames in os.walk(d, onerror=lambda e: None):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    st = os.lstat(fp)
                    if st.st_mtime < cutoff and os.access(fp, os.W_OK):
                        victims.append(fp)
                        total += st.st_size
                except OSError:
                    pass
    print(f"  Folders: {', '.join(str(d) for d in _temp_dirs())}")
    print(f"  {len(victims)} files older than {args.days} days, {human(total)} in total.")
    if not victims:
        return VOICE.say("Nothing to clean.")
    if not args.apply:
        return VOICE.say("Dry run only. Add --apply to delete them.")
    if not confirm(f"Delete {len(victims)} files?", args.yes):
        return VOICE.say("Cancelled.")
    freed = 0
    for fp in victims:
        try:
            size = os.lstat(fp).st_size
            os.remove(fp)
            freed += size
        except OSError:
            pass
    VOICE.say(f"Freed {human(freed)}.")


def cmd_users(args) -> None:
    """Who is logged in."""
    rows = [[u.name, u.terminal or "-", u.host or "local", dt.datetime.fromtimestamp(u.started).strftime("%Y-%m-%d %H:%M")]
            for u in psutil.users()]
    table(rows, ["USER", "TERMINAL", "FROM", "SINCE"]) if rows else VOICE.say("No interactive sessions found.")


def cmd_services(args) -> None:
    """List running services (systemd, launchd or Windows)."""
    if os.name == "nt":
        rows = []
        for s in psutil.win_service_iter():
            try:
                info = s.as_dict()
                if info["status"] == "running" or args.all:
                    rows.append([info["name"], info["status"], info["start_type"], info["display_name"][:40]])
            except psutil.Error:
                pass
        return table(rows, ["NAME", "STATUS", "START", "DESCRIPTION"])
    if shutil.which("systemctl"):
        state = [] if args.all else ["--state=running"]
        sh(["systemctl", "list-units", "--type=service", "--no-pager", *state])
    elif shutil.which("launchctl"):
        sh(["launchctl", "list"])
    else:
        VOICE.say("I don't recognise this system's service manager.")


def cmd_service(args) -> None:
    """Start, stop or restart a service (asks first)."""
    if os.name == "nt":
        cmd = ["sc", "stop" if args.action == "stop" else "start", args.name]
        if args.action == "restart":
            cmd = ["powershell", "-Command", f"Restart-Service -Name '{args.name}'"]
    elif shutil.which("systemctl"):
        cmd = ["systemctl", args.action, args.name]
        if os.geteuid() != 0:
            # pkexec shows a graphical password prompt when there's no terminal (the GUI app)
            use_gui = not sys.stdin.isatty() and shutil.which("pkexec")
            cmd = ["pkexec" if use_gui else "sudo", *cmd]
    elif shutil.which("brew"):
        cmd = ["brew", "services", args.action, args.name]
    else:
        return VOICE.say("I don't know how to manage services here.")
    if confirm(f"Run: {' '.join(cmd)} ?", args.yes):
        rc = sh(cmd)
        VOICE.say(f"{args.name} {args.action} {'done' if rc == 0 else 'failed'}.")


def cmd_watch(args) -> None:
    """Live dashboard; Ctrl+C to stop."""
    net0 = psutil.net_io_counters()
    try:
        while True:
            time.sleep(args.interval)
            net1 = psutil.net_io_counters()
            up = (net1.bytes_sent - net0.bytes_sent) / args.interval
            down = (net1.bytes_recv - net0.bytes_recv) / args.interval
            net0 = net1
            print("\033[2J\033[H", end="")
            print(c(f"  Jarvis live monitor · {dt.datetime.now():%H:%M:%S} · Ctrl+C to stop\n", BOLD))
            for i, pct in enumerate(psutil.cpu_percent(percpu=True)):
                print(f"  CPU{i:<3} {bar(pct, 30)}")
            print(f"\n  Memory {bar(psutil.virtual_memory().percent, 30)}")
            print(f"  Net    ↑ {human(up)}/s   ↓ {human(down)}/s\n")
            top = sorted((i for _, i in processes(["pid", "name", "cpu_percent"])),
                         key=lambda i: i["cpu_percent"] or 0, reverse=True)[:5]
            for i in top:
                print(c(f"  {i['pid']:>7}  {(i['name'] or '?')[:30]:<30} {i['cpu_percent'] or 0:5.1f}%", DIM))
    except KeyboardInterrupt:
        print()


def cmd_run(args) -> None:
    """Run a shell command (asks first)."""
    command = " ".join(args.command)
    if confirm(f"Run `{command}`?", args.yes):
        rc = sh(command, shell=True)
        VOICE.say("Done." if rc == 0 else f"That exited with code {rc}.")


def cmd_report(args) -> None:
    """Save a system snapshot to a text file."""
    import contextlib
    import io

    buf = io.StringIO()
    global USE_COLOR
    saved, USE_COLOR = USE_COLOR, False
    try:
        with contextlib.redirect_stdout(buf):
            for title, fn, ns in [
                ("STATUS", cmd_status, args),
                ("TOP CPU", cmd_top, argparse.Namespace(sort="cpu", n=15)),
                ("TOP MEMORY", cmd_top, argparse.Namespace(sort="mem", n=15)),
                ("NETWORK", cmd_net, args),
                ("LISTENING PORTS", cmd_ports, args),
                ("USERS", cmd_users, args),
            ]:
                print(f"\n===== {title} =====")
                fn(ns)
    finally:
        USE_COLOR = saved
    out = Path(args.output or f"jarvis-report-{dt.datetime.now():%Y%m%d-%H%M%S}.txt")
    out.write_text(buf.getvalue(), encoding="utf-8")
    VOICE.say(f"Report saved to {out.resolve()}.")


# --------------------------------------------------------------------------- #
# Argument parser and natural-language front end
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jarvis", description="Jarvis - manage your system.")
    p.add_argument("--voice", action="store_true", help="speak replies in a British male voice")
    sub = p.add_subparsers(dest="cmd")

    def add(name, fn, *aliases):
        sp = sub.add_parser(name, aliases=list(aliases), help=fn.__doc__)
        sp.set_defaults(func=fn)
        return sp

    add("status", cmd_status, "s")
    sp = add("top", cmd_top)
    sp.add_argument("sort", nargs="?", choices=["cpu", "mem"], default="cpu")
    sp.add_argument("-n", type=int, default=10)
    add("find", cmd_find).add_argument("name")
    sp = add("kill", cmd_kill)
    sp.add_argument("target", help="PID or exact process name")
    sp.add_argument("-f", "--force", action="store_true")
    sp.add_argument("-y", "--yes", action="store_true")
    add("net", cmd_net)
    add("ports", cmd_ports)
    sp = add("ping", cmd_ping)
    sp.add_argument("host", nargs="?", default="8.8.8.8")
    sp.add_argument("-c", "--count", type=int, default=4)
    sp = add("bigfiles", cmd_bigfiles)
    sp.add_argument("path", nargs="?", default=str(Path.home()))
    sp.add_argument("-n", type=int, default=15)
    sp = add("clean", cmd_clean)
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    sp.add_argument("-y", "--yes", action="store_true")
    add("users", cmd_users)
    add("services", cmd_services).add_argument("--all", action="store_true")
    sp = add("service", cmd_service)
    sp.add_argument("action", choices=["start", "stop", "restart"])
    sp.add_argument("name")
    sp.add_argument("-y", "--yes", action="store_true")
    add("watch", cmd_watch).add_argument("-i", "--interval", type=float, default=1.0)
    sp = add("run", cmd_run)
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp.add_argument("-y", "--yes", action="store_true")
    add("report", cmd_report).add_argument("-o", "--output")
    return p


# Plain-English phrases mapped to commands. First match wins.
INTENTS: list[tuple[str, callable]] = [
    (r"\b(kill|terminate|stop|close|end)\b\s+(process\s+)?(?P<t>[\w.\-]+)$", lambda m: ["kill", m["t"]]),
    (r"\b(restart|start|stop)\b\s+(the\s+)?service\s+(?P<n>[\w.@\-]+)",
     lambda m: ["service", m[1].lower(), m["n"]]),
    (r"\bis\s+(?P<n>[\w.\-]+)\s+running", lambda m: ["find", m["n"]]),
    (r"\b(find|search for|search|where is)\b\s+(process\s+)?(?P<n>[\w.\-]+)", lambda m: ["find", m["n"]]),
    (r"\bping\b\s*(?P<h>[\w.\-:]+)?", lambda m: ["ping"] + ([m["h"]] if m["h"] else [])),
    (r"\b(memory|ram)\b.*\b(hog|top|using|eat\w*|hungry)|\b(eat\w*|hog\w*|using)\b.*\b(memory|ram)\b|\btop mem", lambda m: ["top", "mem"]),
    (r"\b(top|cpu hog|slow|busy|processes)\b", lambda m: ["top", "cpu"]),
    (r"\b(port|listening|open ports)\b", lambda m: ["ports"]),
    (r"\b(network|wifi|internet|ip address|ip)\b", lambda m: ["net"]),
    (r"\b(big|biggest|large|largest|huge)\b.*\bfiles?\b(\s+in\s+(?P<p>\S+))?",
     lambda m: ["bigfiles"] + ([m["p"]] if m["p"] else [])),
    (r"\b(clean|junk|temp|cache|free up)\b", lambda m: ["clean"]),
    (r"\b(who|users|logged)\b", lambda m: ["users"]),
    (r"\bservices\b", lambda m: ["services"]),
    (r"\b(watch|monitor|live)\b", lambda m: ["watch"]),
    (r"\breport\b", lambda m: ["report"]),
    (r"\b(status|how.?s|health|overview|system|battery|disk|space)\b", lambda m: ["status"]),
]

COMMANDS = {"status", "s", "top", "find", "kill", "net", "ports", "ping", "bigfiles", "clean",
            "users", "services", "service", "watch", "run", "report"}

HELP = """
  Talk to me in plain English or use commands:
    status                     overview of CPU, memory, disks, battery
    top [cpu|mem]              busiest processes
    find <name>                look up a process
    kill <pid|name> [-f]       stop a process
    net / ports / ping <host>  network info, listening ports, reachability
    bigfiles [path]            largest files in a folder
    clean [--apply]            remove old temp/cache files
    users / services           sessions and running services
    service <start|stop|restart> <name>
    watch                      live monitor
    report                     save a snapshot to a file
    run <shell command>        run anything (asks first)
    exit                       leave
  Examples: "how's my system?", "what's eating my ram", "kill chrome", "open ports"
"""


def _parses(words: list[str]) -> bool:
    import contextlib
    import io

    with contextlib.redirect_stderr(io.StringIO()):
        try:
            build_parser().parse_args(words)
            return True
        except SystemExit:
            return False


def interpret(text: str) -> list[str] | None:
    if text.split()[0].lower() in COMMANDS:  # looks like a typed command
        try:
            words = shlex.split(text, posix=os.name != "nt")
        except ValueError:
            words = text.split()
        if _parses(words):
            return words
    t = text.strip(" ?!.")
    for pattern, make in INTENTS:
        m = re.search(pattern, t, re.IGNORECASE)
        if m:
            return make(m)
    return None


def greeting() -> str:
    h = dt.datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    return f"Good {part}. Jarvis online. How can I help?"


def repl(parser: argparse.ArgumentParser) -> None:
    VOICE.say(greeting())
    print(c("  Type 'help' for what I can do.", DIM))
    while True:
        try:
            text = input(c("\nyou › ", GREEN)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit", "bye", "goodbye"):
            break
        if text.lower() in ("help", "?", "what can you do"):
            print(HELP)
            continue
        try:
            argv = interpret(text)
        except ValueError:
            argv = None
        if not argv:
            VOICE.say("Sorry, I didn't catch that. Type 'help' for examples.")
            continue
        try:
            ns = parser.parse_args(argv)
            ns.func(ns)
        except SystemExit:
            pass
        except KeyboardInterrupt:
            print(c("\n  Interrupted.", DIM))
        except Exception as e:  # keep the session alive
            print(c(f"  Something went wrong: {e}", RED))
    VOICE.say("Goodbye.")


def main(argv: list[str] | None = None) -> None:
    global VOICE
    parser = build_parser()
    args = parser.parse_args(argv)
    VOICE = Voice(args.voice)
    if args.cmd is None:
        repl(parser)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
