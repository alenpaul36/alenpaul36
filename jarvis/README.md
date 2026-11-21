# Jarvis

A small command-line assistant for looking after your own computer. Ask it things in plain
English ("how's my system?", "what's eating my ram", "kill chrome") or use short commands.
Works on Linux, macOS and Windows.

## Setup

```bash
pip install -r requirements.txt
python jarvis.py
```

For spoken replies, also `pip install pyttsx3` and start with `python jarvis.py --voice`.

## What it can do

| Command | What it does |
| --- | --- |
| `status` | CPU, memory, disks, battery, temperatures and uptime, with warnings when something is above 90% |
| `top [cpu\|mem] [-n 10]` | Busiest processes |
| `find <name>` | Look up a process by name or command line |
| `kill <pid\|name> [-f]` | Stop a process (asks first) |
| `net` | Interfaces, IP addresses, traffic, internet check |
| `ports` | Listening ports, which program owns each, and whether it is exposed beyond localhost |
| `ping [host]` | Check whether a host is reachable |
| `bigfiles [path] [-n 15]` | Largest files under a folder |
| `clean [--days 7] [--apply]` | Find old temp and cache files; deletes only with `--apply` |
| `users` | Who is logged in |
| `services [--all]` / `service <start\|stop\|restart> <name>` | List and control services (systemd, Homebrew, Windows) |
| `watch [-i 1]` | Live per-core CPU, memory, network and top-process monitor |
| `report [-o file]` | Save a full snapshot to a text file |
| `run <command>` | Run any shell command (asks first) |

Run any command directly, e.g. `python jarvis.py ports`, or start the interactive prompt
with no arguments. Anything that changes the system (`kill`, `clean --apply`, `service`, `run`)
asks for confirmation; add `-y` to skip it in scripts. Listing ports or controlling services may
need `sudo` or an administrator terminal.
