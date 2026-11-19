#!/usr/bin/env python3
"""Hourly low-ink check (run from cron).

Reads each printer's ink levels over IPP and sends one "ink_low" notification
when an ink drops to or below the alert level (global "ink_alert_percent",
default 15, 0 = off). That ink is not alerted again until it is refilled
(rises 10 points above the alert level).
"""

import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pbs_state import DATA_DIR, load_printers, locked, read_json, write_json  # noqa: E402
from printer_probe import probe_printer  # noqa: E402

STATE_FILE = f"{DATA_DIR}/ink-alerts.json"
LOG_FILE = f"{DATA_DIR}/logs/auto-print.log"
NOTIFY = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.py")]
UNRAID_NOTIFY = "/unraid/notify"
REARM_MARGIN = 10


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ink-check] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main():
    data = load_printers()
    try:
        threshold = int(data.get("global", {}).get("ink_alert_percent", 15))
    except (TypeError, ValueError):
        threshold = 15
    if threshold <= 0:
        return 0

    # One check per physical printer, skipping printers whose entries are all paused
    printers = {}
    for p in data.get("printers", []):
        if p.get("ip") and not p.get("paused"):
            printers.setdefault(p["ip"], p)

    with locked(STATE_FILE):
        state = read_json(STATE_FILE, {})
        for ip, p in printers.items():
            info = probe_printer(ip, p.get("connection", "ipp"), p.get("port", 9100))
            inks = [i for i in info.get("ink_levels", []) if isinstance(i.get("level"), int) and i["level"] >= 0]
            if not inks:
                continue
            alerted = state.setdefault(ip, {})
            newly_low = []
            for ink in inks:
                name, level = ink["name"], ink["level"]
                if level <= threshold and not alerted.get(name):
                    newly_low.append(f"{name} {level}%")
                elif level > threshold + REARM_MARGIN and alerted.get(name):
                    alerted.pop(name)
                    log(f"{ip}: {name} back to {level}%, alert re-armed")
            if not newly_low:
                continue
            title = info.get("model") or p.get("name") or ip
            message = f"Below {threshold}%: " + ", ".join(newly_low)
            result = subprocess.run(NOTIFY + ["--event", "ink_low", "--printer", title,
                                              "--printer-id", p["id"], "--message", message],
                                    capture_output=True, text=True, timeout=60)
            if result.stderr:
                with open(LOG_FILE, "a") as f:
                    f.write(result.stderr)
            if os.access(UNRAID_NOTIFY, os.X_OK):
                subprocess.run([UNRAID_NOTIFY, "-s", f"Ink low — {title}", "-d", message, "-i", "warning"],
                               capture_output=True, timeout=30)
            if result.returncode == 0:
                for item in newly_low:
                    alerted[item.rsplit(" ", 1)[0]] = True
                log(f"{title}: {message}")
            else:
                log(f"{title}: {message} (notification failed, will retry next hour)")
        write_json(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
