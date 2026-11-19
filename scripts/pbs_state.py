#!/usr/bin/env python3
"""Shared state and scheduling helpers for NozzleWake.

Used by auto-print.sh (as a CLI), webui.py and ink-check.py (as a module).

Scheduling model
----------------
A printer prints every `interval_days` days at `hour`:`minute`. Cron runs
auto-print.sh for it every day at that time; the script only prints when the
last successful maintenance (or detected real use) is at least interval_days
old. This avoids cron's day-of-month steps (*/5 fires on the 31st and again on
the 1st) and means a failed or missed run is retried the next day.

Printers whose schedule is a cron expression we can't map keep that cron
expression as-is ("legacy" mode, no due check).

CLI:
    pbs_state.py migrate                  convert cron schedules to intervals
    pbs_state.py crontab                  install the crontab from printers.json
    pbs_state.py due <id>                 exit 0 if a scheduled print is due
    pbs_state.py mark <id> <source> [epoch] [media_type]
    pbs_state.py status <id> <status> <message>
    pbs_state.py history <id> <result> <message>
    pbs_state.py streak <id>              consecutive failed runs
    pbs_state.py activity <id>            "<epoch>|<description>" of last real use
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DATA_DIR = os.environ.get("PBS_DATA_DIR", "/data")
PRINTERS_FILE = f"{DATA_DIR}/printers.json"
HISTORY_FILE = f"{DATA_DIR}/print-history.json"
LEDGER_FILE = f"{DATA_DIR}/maintained.json"
HISTORY_MAX = 500
DUE_TOLERANCE = 2 * 3600  # a run at 10:00:05 counts yesterday's 10:00:40 print as a day old
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "600") or 600)
INK_CHECK_CRON = "17 * * * *"
CRON_SUFFIX = ">> /data/logs/cron.log 2>&1"

# Paper types after which Epson's two-black printers use Photo Black; everything
# else (plain, matte, fine art, envelopes) uses Matte Black. Printers with one
# black ink are unaffected by the distinction.
PHOTO_BLACK_TYPES = {"photographic", "photographic-glossy", "photographic-high-gloss",
                     "photographic-semi-gloss", "com.epson-luster"}


def now_epoch():
    return int(time.time())


def ts(epoch=None):
    return datetime.fromtimestamp(epoch or time.time()).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def locked(path):
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_printers():
    return read_json(PRINTERS_FILE, {"printers": [], "global": {}})


def find_printer(data, pid):
    return next((p for p in data.get("printers", []) if p.get("id") == pid), None)


CRON_FIELD = r"[0-9*/,\-]{1,40}"
CRON_RE = re.compile(rf"^{CRON_FIELD}( {CRON_FIELD}){{4}}$")
PRINTER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_cron(expr):
    """Five plain cron fields only: digits, * / , - (nothing a shell could act on)."""
    return isinstance(expr, str) and bool(CRON_RE.fullmatch(expr))


def media_class(media_type):
    return "photo-black" if (media_type or "") in PHOTO_BLACK_TYPES else "matte-black"


# ── Schedules ────────────────────────────────────────────────────

def parse_schedule(cron):
    """Map the cron expressions the dashboard used to generate to (days, hour, minute)."""
    m = re.fullmatch(r"(\d{1,2}) (\d{1,2}) (\S+) \* (\S+)", (cron or "").strip())
    if not m:
        return None
    minute, hour, dom, dow = int(m[1]), int(m[2]), m[3], m[4]
    if minute > 59 or hour > 23:
        return None
    if dom == "*" and dow == "*":
        days = 1
    elif dow == "*" and re.fullmatch(r"\*/\d{1,3}", dom):
        days = int(dom[2:])
    elif dow == "*" and dom == "1,15":
        days = 14
    elif dom == "*" and re.fullmatch(r"[0-7]", dow):
        days = 7
    else:
        return None
    return (days, hour, minute) if 1 <= days <= 365 else None


def cron_for(days, hour, minute=0):
    """Cron expression stored for display/back-compat; scheduling uses the interval."""
    if days == 1:
        return f"{minute} {hour} * * *"
    return f"{minute} {hour} */{days} * *"


def is_interval(p):
    return isinstance(p.get("interval_days"), int)


def set_interval(p, days, hour, minute=0):
    p["interval_days"], p["hour"], p["minute"] = int(days), int(hour), int(minute)
    p["schedule"] = cron_for(p["interval_days"], p["hour"], p["minute"])


def migrate_printer(p, default_schedule="0 10 */3 * *"):
    if is_interval(p):
        return False
    parsed = parse_schedule(p.get("schedule") or default_schedule)
    if not parsed:
        return False
    set_interval(p, *parsed)
    return True


def migrate(data):
    default = data.get("global", {}).get("schedule", "0 10 */3 * *")
    return any([migrate_printer(p, default) for p in data.get("printers", [])])


def crontab_text(data):
    lines = []
    for p in data.get("printers", []):
        if p.get("paused"):
            continue
        if is_interval(p):
            sched = f'{int(p.get("minute", 0))} {int(p["hour"])} * * *'
        else:
            sched = p.get("schedule") or data.get("global", {}).get("schedule", "0 10 */3 * *")
        # Each line runs as root: refuse anything but a plain schedule and a safe id.
        if not valid_cron(sched) or not PRINTER_ID_RE.fullmatch(str(p.get("id", ""))):
            print(f"Skipping printer {p.get('id')!r}: invalid schedule or id", file=sys.stderr)
            continue
        lines.append(f'{sched} . /etc/environment; /app/auto-print.sh --printer-id={p["id"]} {CRON_SUFFIX}')
    lines.append(f"{INK_CHECK_CRON} . /etc/environment; python3 /app/ink-check.py {CRON_SUFFIX}")
    return "\n".join(lines) + "\n"


def install_crontab(data):
    try:
        subprocess.run(["crontab", "-"], input=crontab_text(data).encode(), capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"Could not install crontab: {e}", file=sys.stderr)


# ── Ledger: when was each printer entry last maintained ─────────────

def history_entries():
    out = []
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return out


def last_maintained(pid):
    """(epoch, record) of the last successful print or detected real use; (0, {}) if never."""
    rec = read_json(LEDGER_FILE, {}).get(pid)
    if rec and rec.get("epoch"):
        return int(rec["epoch"]), rec
    for e in reversed(history_entries()):  # before the ledger existed
        if e.get("printer_id") == pid and e.get("result") == "ok" and e.get("epoch"):
            return int(e["epoch"]), {"epoch": int(e["epoch"]), "source": "print"}
    return 0, {}


def mark(pid, source, epoch=None, media_type=""):
    epoch = int(epoch or now_epoch())
    with locked(LEDGER_FILE):
        ledger = read_json(LEDGER_FILE, {})
        if int(ledger.get(pid, {}).get("epoch", 0)) <= epoch:
            ledger[pid] = {"epoch": epoch, "source": source, "media_type": media_type}
            write_json(LEDGER_FILE, ledger)


def is_due(p, now=None):
    if not is_interval(p):
        return True, "custom cron schedule"
    now = now or now_epoch()
    last, rec = last_maintained(p["id"])
    if not last:
        return True, "never maintained"
    age = now - last
    need = p["interval_days"] * 86400 - DUE_TOLERANCE
    if age >= need:
        return True, f"last maintained {age / 86400:.1f} days ago"
    return False, f"last maintained {age / 86400:.1f} days ago ({rec.get('source', 'print')}), every {p['interval_days']} days"


def _cron_next(cron_expr, now):
    """Next fire time for the simple cron expressions the old dashboard generated."""
    parts = cron_expr.strip().split()
    if len(parts) < 5:
        return None
    try:
        minute = int(parts[0]) if parts[0] != "*" else 0
        hour = int(parts[1]) if parts[1] != "*" else 0
    except ValueError:
        return None
    dom, dow = parts[2], parts[4]
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    for _ in range(400):
        ok = True
        if dow != "*" and str((cand.weekday() + 1) % 7) not in dow.split(","):
            ok = False
        elif dom.startswith("*/"):
            step = int(dom[2:] or 1)
            ok = step > 0 and (cand.day - 1) % step == 0
        elif dom != "*":
            ok = str(cand.day) in dom.split(",")
        if ok:
            return cand
        cand += timedelta(days=1)
    return None


def next_run(p, data=None, now=None):
    """datetime of the next scheduled maintenance print, or None if paused/unknown."""
    if p.get("paused"):
        return None
    now = now or datetime.now()
    if not is_interval(p):
        default = (data or {}).get("global", {}).get("schedule", "0 10 */3 * *")
        return _cron_next(p.get("schedule") or default, now)
    last, _ = last_maintained(p["id"])
    cand = now.replace(hour=p["hour"], minute=p.get("minute", 0), second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    need = p["interval_days"] * 86400 - DUE_TOLERANCE
    while last and cand.timestamp() - last < need:
        cand += timedelta(days=1)
    return cand


# ── Status / history ─────────────────────────────────────────────

def write_status(pid, status, message):
    write_json(f"{DATA_DIR}/status-{pid}.json",
               {"status": status, "message": message, "timestamp": ts(), "printer_id": pid})


def write_history(pid, result, message):
    entry = {"timestamp": ts(), "epoch": now_epoch(), "result": result,
             "message": message, "printer_id": pid}
    with locked(HISTORY_FILE):
        lines = []
        try:
            with open(HISTORY_FILE) as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
        except FileNotFoundError:
            pass
        lines = (lines + [json.dumps(entry)])[-HISTORY_MAX:]
        tmp = f"{HISTORY_FILE}.tmp{os.getpid()}"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, HISTORY_FILE)


def failure_streak(pid):
    """Consecutive failed runs, newest first ("skipped" runs don't count either way)."""
    streak = 0
    for e in reversed(history_entries()):
        if e.get("printer_id") != pid or e.get("result") == "skipped":
            continue
        if e.get("result") != "error":
            break
        streak += 1
    return streak


# ── Real-use detection ───────────────────────────────────────────

def own_print_epochs(ip, data):
    """When this container finished maintenance prints on the printer at `ip`."""
    ids = {p["id"] for p in data.get("printers", []) if p.get("ip") == ip}
    return [int(e["epoch"]) for e in history_entries()
            if e.get("printer_id") in ids and e.get("result") == "ok" and e.get("epoch")]


def last_activity(p, data, timeout=6):
    """(epoch, description) of the most recent use of this printer that exercised the
    same black ink as this entry's paper type, or (0, reason) if none found.

    Sources: other entries for the same printer in this container, and the printer's
    own list of completed jobs (prints from PCs, phones, etc.).
    """
    want = media_class(p.get("media_type", ""))
    best, desc = 0, "no recent use found"

    ledger = read_json(LEDGER_FILE, {})
    for q in data.get("printers", []):
        if q["id"] == p["id"] or q.get("ip") != p.get("ip"):
            continue
        rec = ledger.get(q["id"], {})
        if rec.get("source") == "print" and media_class(rec.get("media_type", "")) == want \
                and int(rec.get("epoch", 0)) > best:
            best, desc = int(rec["epoch"]), f'maintenance print by "{q.get("name", q["id"])}"'

    if p.get("connection", "ipp") == "ipp":
        try:
            from ipp_client import completed_jobs
            own = own_print_epochs(p["ip"], data)
            for job in completed_jobs(p["ip"], timeout=timeout):
                when = job["completed"]
                if not when:
                    continue
                epoch = int(when.timestamp())
                if job["name"].startswith("pbs-maintenance"):
                    continue
                # job names are often hidden; also ignore jobs that finished while we were printing
                if any(e - JOB_TIMEOUT - 60 <= epoch <= e + 60 for e in own):
                    continue
                if media_class(job["media_type"]) != want:
                    continue
                if epoch > best:
                    paper = job["media_type"] or "paper type not reported"
                    best, desc = epoch, f"printer reports job #{job['id']} completed ({paper})"
                break  # newest matching job is enough
        except Exception as e:  # printer doesn't support it, or is unreachable
            if not best:
                desc = f"printer job history unavailable ({type(e).__name__})"
    return best, desc


# ── CLI ──────────────────────────────────────────────────────────

def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "migrate":
        with locked(PRINTERS_FILE):
            data = load_printers()
            if migrate(data):
                write_json(PRINTERS_FILE, data)
                print("Converted cron schedules to day intervals")
        return 0
    if cmd == "crontab":
        data = load_printers()
        install_crontab(data)
        print(f"  {sum(1 for p in data.get('printers', []) if not p.get('paused'))} printer schedule(s) installed")
        return 0
    if cmd == "status":
        write_status(args[0], args[1], args[2] if len(args) > 2 else "")
        return 0
    if cmd == "history":
        write_history(args[0], args[1], args[2] if len(args) > 2 else "")
        return 0
    if cmd == "streak":
        print(failure_streak(args[0]))
        return 0
    if cmd == "mark":
        mark(args[0], args[1], int(args[2]) if len(args) > 2 and args[2] else None,
             args[3] if len(args) > 3 else "")
        return 0

    data = load_printers()
    p = find_printer(data, args[0]) if args else None
    if not p:
        print(f"unknown printer {args[0] if args else ''}", file=sys.stderr)
        return 2
    if cmd == "due":
        due, reason = is_due(p)
        print(reason)
        return 0 if due else 1
    if cmd == "activity":
        epoch, desc = last_activity(p, data)
        print(f"{epoch}|{desc}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main(sys.argv[1:]))
