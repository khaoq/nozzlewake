#!/usr/bin/env python3
"""
Web UI for NozzleWake (formerly print-blockage-stopper).
Full-featured dashboard with:
  - Multi-printer management (add/remove/rename)
  - Network discovery (mDNS/IPP)
  - Per-printer status, schedule, skip-hours, pause/resume
  - Test connection / auto-detect model via IPP
  - Connection status indicators (periodic ping)
  - Ink level display (IPP marker-levels)
  - Test image preview thumbnails
  - Print history chart (30 days) + CSV export
  - Custom test image upload
  - Webhook notification config
  - Mobile-friendly layout
"""

import base64
import csv
import fcntl
import hmac
import http.server
import io
import json
import html as html_mod
import os
import re
import subprocess
import uuid
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pbs_state  # noqa: E402
from notify import validate_url as notify_validate_url  # noqa: E402

PORT = 8631
# Optional: when set, every page and API call needs HTTP Basic auth with this
# password (any username). /healthz stays open for the container healthcheck.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DATA_DIR = "/data"
PRINTERS_FILE = f"{DATA_DIR}/printers.json"
HISTORY_FILE = f"{DATA_DIR}/print-history.json"
LOG_FILE = f"{DATA_DIR}/logs/auto-print.log"
UPLOADS_DIR = f"{DATA_DIR}/uploads"
PRESETS_DIR = "/app/presets"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
THUMBS_DIR = f"{DATA_DIR}/thumbnails"

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(THUMBS_DIR, exist_ok=True)

# ── Printer config helpers ──────────────────────────────────────

def read_printers():
    try:
        with open(PRINTERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"printers": [], "global": {"schedule": os.environ.get("SCHEDULE", "0 10 */3 * *"),
                                            "skip_hours": int(os.environ.get("SKIP_HOURS", "72")),
                                            "webhook_url": os.environ.get("WEBHOOK_URL", "")}}

SECRET_SENTINEL = "***"

# Common IPP media-type keywords. The dashboard swaps in whatever the printer
# itself reports (media-type-supported) once it has probed it.
# Friendly names for IPP media-type keywords; types that make Epson's two-black
# printers use Photo Black are marked.
_MEDIA_NAMES = {
    "": "Printer default",
    "stationery": "Plain paper",
    "stationery-coated": "Plain paper (coated)",
    "stationery-lightweight": "Plain paper (lightweight)",
    "stationery-letterhead": "Letterhead",
    "photographic": "Photo paper",
    "photographic-glossy": "Photo paper (glossy)",
    "photographic-high-gloss": "Photo paper (high gloss)",
    "photographic-semi-gloss": "Photo paper (semi-gloss)",
    "photographic-matte": "Photo paper (matte)",
    "com.epson-luster": "Epson Luster photo paper",
    "com.epson-velvet-fine-art": "Epson Velvet Fine Art",
    "com.epson-thickpaper1": "Thick paper 1",
    "com.epson-thickpaper2": "Thick paper 2",
    "envelope": "Envelope",
    "disc": "Disc (CD/DVD)",
}
MEDIA_LABELS = {k: v + (" \u00b7 Photo Black" if k in pbs_state.PHOTO_BLACK_TYPES else "")
                for k, v in _MEDIA_NAMES.items()}
# Shown until the printer reports its own list
MEDIA_TYPES = [(k, MEDIA_LABELS[k]) for k in
               ("", "stationery", "photographic-glossy", "photographic-high-gloss",
                "photographic-semi-gloss", "photographic-matte")]
MEDIA_TYPE_RE = re.compile(r"^[a-z0-9_.-]{0,64}$")
HOST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9]$")
PAPER_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")
PRESET_RE = re.compile(r"preset-[a-z0-9]+(?:-[a-z0-9]+){0,4}")
PRESET_LABELS = {"et8500-plain": "ET-8500 page 1 · plain",
                 "et8500-photo": "ET-8500 page 2 · Photo Black (A4)",
                 "et8500-photo-4x6": "ET-8500 page 2 · Photo Black (4×6)"}
MAX_JSON_BYTES = 64 * 1024

# DNS-rebinding protection: only answer requests addressed to this server by IP,
# a LAN-style hostname, or a name listed in ALLOWED_HOSTS (comma separated, e.g.
# "printers.example.com,.home.example.com"; "*" disables the check).
ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".localhost")

def host_allowed(host_header):
    if "*" in ALLOWED_HOSTS:
        return True
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [IPv6]:port
        name = host[1:host.find("]")] if "]" in host else ""
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".")
    if not name:
        return False
    try:
        import ipaddress
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or "." not in name or name.endswith(LAN_SUFFIXES):
        return True
    return any(name == a or (a.startswith(".") and name.endswith(a)) for a in ALLOWED_HOSTS)

def valid_test_image(img):
    """True if img names an existing preset or uploaded image (no paths)."""
    if not isinstance(img, str):
        return False
    if PRESET_RE.fullmatch(img):
        return os.path.isfile(f"{PRESETS_DIR}/{img}.png")
    if img.startswith("custom-") and UPLOAD_NAME_RE.fullmatch(img[7:]):
        return os.path.isfile(os.path.join(UPLOADS_DIR, img[7:]))
    return False

def clean_printer_fields(body):
    """Validate the editable printer fields present in body. Returns (dict, error)."""
    out = {}
    if "name" in body:
        name = str(body["name"]).strip()
        name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:64]
        if not name:
            return None, "Name cannot be empty"
        out["name"] = name
    if "ip" in body:
        ip = str(body["ip"]).strip()
        if not HOST_RE.match(ip):
            return None, "Invalid IP address or hostname"
        out["ip"] = ip
    if "connection" in body:
        if body["connection"] not in ("ipp", "socket"):
            return None, "Connection must be ipp or socket"
        out["connection"] = body["connection"]
    if "port" in body:
        port = clean_int(body["port"], 1, 65535)
        if port is None:
            return None, "Port must be 1-65535"
        out["port"] = port
    if "paper_size" in body:
        paper = str(body["paper_size"]).strip()
        if not PAPER_RE.match(paper):
            return None, "Invalid paper size"
        out["paper_size"] = paper
    if "media_type" in body:
        mt = clean_media_type(body["media_type"])
        if mt is None:
            return None, "Invalid paper type"
        out["media_type"] = mt
    if "test_image" in body:
        if not valid_test_image(body["test_image"]):
            return None, "Unknown test image"
        out["test_image"] = body["test_image"]
    if "skip_hours" in body:
        hours = clean_int(body["skip_hours"], 0, 8760)
        if hours is None:
            return None, "Skip hours must be 0-8760"
        out["skip_hours"] = hours
    if "paused" in body:
        if not isinstance(body["paused"], bool):
            return None, "paused must be true or false"
        out["paused"] = body["paused"]
    return out, None

def clean_media_type(value):
    value = str(value or "").strip()
    return value if MEDIA_TYPE_RE.match(value) else None

def parse_webhook_urls(value):
    """The webhook setting may hold several URLs (one per line, or comma separated)."""
    return [u for u in re.split(r"[\s,]+", value or "") if u]

def redact_config(data):
    """Copy of the config safe to return over the unauthenticated API.

    Credentials (and the webhook URL) are replaced with the same "***" sentinel the dashboard
    form uses, so the save handlers keep the stored value when it is sent back.
    """
    out = json.loads(json.dumps(data))
    g = out.get("global", {})
    if g.get("webhook_url"):
        g["webhook_url"] = SECRET_SENTINEL  # Slack/Discord URLs embed their token
    for section, key in (("email", "smtp_password"), ("homeassistant", "ha_token")):
        cfg = g.get(section)
        if isinstance(cfg, dict) and cfg.get(key):
            cfg[key] = SECRET_SENTINEL
    return out

def write_printers(data):
    with open(PRINTERS_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(data, f, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def sanitise_cups_name(name):
    s = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    return s[:64] or "PRINTER"

# ── CUPS management ─────────────────────────────────────────────

def add_cups_printer(printer):
    cups_name = printer["cups_name"]
    conn = printer.get("connection", "ipp")
    ip = printer["ip"]
    port = printer.get("port", 9100)
    uri = f"socket://{ip}:{port}" if conn == "socket" else f"ipp://{ip}/ipp/print"
    paper = printer.get("paper_size", "A4")
    try:
        subprocess.run(
            ["lpadmin", "-p", cups_name, "-v", uri, "-m", "everywhere",
             "-L", "Network", "-D", printer.get("name", cups_name),
             "-o", f"media={paper}", "-o", "printer-error-policy=abort-job",
             "-o", "printer-is-shared=false"],
            check=True, capture_output=True, timeout=15)
        subprocess.run(["cupsaccept", cups_name], capture_output=True, timeout=5)
        subprocess.run(["cupsenable", cups_name], capture_output=True, timeout=5)
        return True
    except Exception as e:
        print(f"Error adding CUPS printer {cups_name}: {e}")
        return False

def remove_cups_printer(cups_name):
    try:
        subprocess.run(["lpadmin", "-x", cups_name], capture_output=True, timeout=10)
    except Exception:
        pass

# ── Print queue ─────────────────────────────────────────────────

def get_queue(cups_name):
    """Jobs still waiting in CUPS for this printer, oldest first."""
    try:
        result = subprocess.run(["lpstat", "-o", cups_name],
                                capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    jobs = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].startswith(f"{cups_name}-"):
            jobs.append({"id": parts[0], "user": parts[1], "size": parts[2],
                         "submitted": parts[3].strip()})
    return jobs

def cancel_queue(cups_name, job_id=None):
    """Cancel one job (if job_id) or every queued job for this printer."""
    if job_id is None:
        cmd = ["cancel", "-a", cups_name]
    elif re.fullmatch(re.escape(cups_name) + r"-\d+", job_id):
        cmd = ["cancel", job_id]
    else:
        return False
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

# ── Printer probing ─────────────────────────────────────────────

def probe_printer(ip, connection="ipp", port=9100):
    """Probe printer for connectivity, model name, and ink levels."""
    try:
        result = subprocess.run(
            ["python3", "/app/printer_probe.py", ip, connection, str(port)],
            capture_output=True, text=True, timeout=20)
        return json.loads(result.stdout)
    except Exception as e:
        return {"reachable": False, "model": None, "ink_levels": [], "error": str(e)}

# ── mDNS / IPP discovery ───────────────────────────────────────

def discover_printers():
    found = []
    try:
        result = subprocess.run(
            ["avahi-browse", "-t", "-r", "-p", "_ipp._tcp"],
            capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            parts = line.split(";")
            if len(parts) < 10 or parts[0] != "=":
                continue
            name = parts[3].replace("\\032", " ")
            ip = parts[7]
            port = parts[8]
            if ":" in ip and ip.startswith("fe80"):
                continue
            if ip and not any(f["ip"] == ip for f in found):
                found.append({"name": name, "ip": ip, "port": int(port) if port.isdigit() else 631})
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    if not found:
        try:
            result = subprocess.run(
                ["ippfind", "--timeout", "5"],
                capture_output=True, text=True, timeout=10)
            for line in result.stdout.strip().splitlines():
                m = re.match(r'ipp://([^:/]+)', line)
                if m:
                    ip = m.group(1)
                    if not any(f["ip"] == ip for f in found):
                        found.append({"name": f"Printer at {ip}", "ip": ip, "port": 631})
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return found

# ── Print helpers ───────────────────────────────────────────────

def get_test_image_path(printer):
    img = printer.get("test_image", "preset-11")
    if valid_test_image(img):  # never a path outside the presets/uploads folders
        return f"{PRESETS_DIR}/{img}.png" if img.startswith("preset-") else os.path.join(UPLOADS_DIR, img[7:])
    if os.path.exists("/app/test-print.png"):
        return "/app/test-print.png"
    return f"{PRESETS_DIR}/preset-11.png"

def trigger_print(printer_id):
    subprocess.Popen(
        ["/app/auto-print.sh", "--force", f"--printer-id={printer_id}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ})

def trigger_print_all():
    data = read_printers()
    for p in data["printers"]:
        trigger_print(p["id"])

# ── Status / history / logs ─────────────────────────────────────

def get_printer_status(printer_id):
    try:
        with open(f"{DATA_DIR}/status-{printer_id}.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "unknown", "message": "No prints yet", "timestamp": "\u2014"}

def get_history():
    entries = []
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        pass
    return entries

def get_recent_logs(lines=25):
    try:
        with open(LOG_FILE) as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except FileNotFoundError:
        return "No log entries yet."

def get_available_presets():
    presets = []
    if os.path.isdir(PRESETS_DIR):
        for f in sorted(os.listdir(PRESETS_DIR)):
            if f.endswith(".png") and f.startswith("preset-"):
                name = f.replace(".png", "")
                if not PRESET_RE.fullmatch(name):
                    continue
                rest = name[len("preset-"):]
                label = PRESET_LABELS.get(rest, f"{rest}-colour" if rest.isdigit() else rest)
                presets.append({"id": name, "label": label, "file": f})
    return presets

def get_uploaded_images():
    images = []
    if os.path.isdir(UPLOADS_DIR):
        for f in sorted(os.listdir(UPLOADS_DIR)):
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".tiff")):
                images.append({"id": f"custom-{f}", "label": f, "file": f})
    return images

def get_image_thumbnail_b64(printer):
    """Return a small base64 data URI for the printer's test image, with disk caching."""
    path = get_test_image_path(printer)
    try:
        # Cache key based on image path and mtime
        src_mtime = os.path.getmtime(path)
        cache_key = re.sub(r'[^a-zA-Z0-9_-]', '_', os.path.basename(path))
        cache_path = os.path.join(THUMBS_DIR, f"{cache_key}.png")

        # Return cached thumbnail if source hasn't changed
        if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= src_mtime:
            with open(cache_path, "rb") as f:
                return f"data:image/png;base64,{base64.b64encode(f.read()).decode()}"

        from PIL import Image as PILImage
        img = PILImage.open(path)
        img.thumbnail((120, 80))
        img.save(cache_path, format="PNG")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"
    except Exception:
        # Clean up partial cache file if save failed
        if 'cache_path' in dir() and os.path.exists(cache_path):
            try:
                os.remove(cache_path)
            except OSError:
                pass
        return ""

# ── Cron management ─────────────────────────────────────────────

def update_cron():
    pbs_state.install_crontab(read_printers())

def clean_int(value, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None

# ── HTTP Handler ────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _authorised(self):
        """Enforce DASHBOARD_PASSWORD if set; sends the 401 itself when not."""
        if not DASHBOARD_PASSWORD or self.path == "/healthz":
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, password = base64.b64decode(header[6:]).decode().partition(":")
                if hmac.compare_digest(password.encode(), DASHBOARD_PASSWORD.encode()):
                    return True
            except ValueError:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="NozzleWake"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _host_ok(self):
        if self.path == "/healthz" or host_allowed(self.headers.get("Host", "")):
            return True
        body = (f"Host '{self.headers.get('Host', '')}' is not allowed. If you reach NozzleWake through this "
                "name (e.g. a reverse proxy), add it to the ALLOWED_HOSTS environment variable.").encode()
        self.send_response(421)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        if not self._host_ok() or not self._authorised():
            return
        if self.path == "/healthz":
            self._json_response({"ok": True})
            return
        static = {"/favicon.ico": ("/app/favicon.ico", "image/x-icon")}.get(self.path.split("?", 1)[0])
        if static:
            try:
                with open(static[0], "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", static[1])
                self.send_header("Content-Length", len(data))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_error(404)
            return
        if self.path == "/":
            self._serve_dashboard()
        elif self.path == "/api/printers":
            self._json_response(redact_config(read_printers()))
        elif self.path == "/api/discover":
            self._json_response({"printers": discover_printers()})
        elif self.path == "/api/history":
            self._json_response({"history": get_history()})
        elif self.path == "/api/history.csv":
            self._serve_history_csv()
        elif self.path == "/api/logs":
            self._json_response({"logs": get_recent_logs(30)})
        elif self.path == "/api/presets":
            self._json_response({"presets": get_available_presets(), "uploads": get_uploaded_images()})
        elif self.path.startswith("/api/status/"):
            pid = self.path.split("/")[-1]
            self._json_response(get_printer_status(pid))
        elif self.path.startswith("/api/next-print/"):
            pid = self.path.split("/")[-1]
            data = read_printers()
            printer = next((p for p in data["printers"] if p["id"] == pid), None)
            last, rec = pbs_state.last_maintained(pid)
            nxt = pbs_state.next_run(printer, data) if printer else None
            self._json_response({
                "next_iso": nxt.isoformat() if nxt else None,
                "paused": bool(printer and printer.get("paused")),
                "last_epoch": last or None,
                "last_source": rec.get("source"),
            })
        elif self.path.startswith("/api/queue/"):
            printer = self._find_printer(self.path.split("/")[-1])
            if printer:
                self._json_response({"jobs": get_queue(printer["cups_name"])})
            else:
                self._json_response({"ok": False, "message": "Unknown printer"}, 404)
        elif self.path.startswith("/api/probe/"):
            ip = self.path.split("/")[-1]
            printer = next((p for p in read_printers()["printers"] if p.get("ip") == ip), None)
            if not printer:  # only probe printers that are configured
                self._json_response({"reachable": False, "error": "Unknown printer"}, 404)
            else:
                self._json_response(probe_printer(ip, printer.get("connection", "ipp"), printer.get("port", 9100)))
        elif self.path.startswith("/api/download-image/"):
            pid = self.path.split("/")[-1]
            data = read_printers()
            printer = next((p for p in data["printers"] if p["id"] == pid), None)
            if printer:
                path = get_test_image_path(printer)
                if os.path.exists(path):
                    fname = os.path.basename(path)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                    with open(path, "rb") as f:
                        img_data = f.read()
                    self.send_header("Content-Length", str(len(img_data)))
                    self.end_headers()
                    self.wfile.write(img_data)
                    return
            self.send_error(404)
        elif self.path.startswith("/api/thumbnail/"):
            pid = self.path.split("/")[-1]
            data = read_printers()
            for p in data["printers"]:
                if p["id"] == pid:
                    self._json_response({"thumbnail": get_image_thumbnail_b64(p)})
                    return
            self._json_response({"thumbnail": ""})
        else:
            self.send_error(404)

    def _check_origin(self):
        """CSRF protection: POSTs must carry the dashboard's custom header (which other
        sites can't send without a CORS preflight we never approve) and, if the browser
        sends an Origin, it must match the Host."""
        if self.headers.get("X-NozzleWake") != "1":
            self._json_response({"ok": False, "message": "Missing X-NozzleWake header"}, 403)
            return False
        origin = self.headers.get("Origin", "")
        if origin:
            from urllib.parse import urlparse
            parsed = urlparse(origin)
            host_header = self.headers.get("Host", "")
            # Allow if origin host matches the Host header (same-origin)
            if parsed.netloc and host_header and parsed.netloc != host_header:
                self._json_response({"ok": False, "message": "Cross-origin request blocked"}, 403)
                return False
        return True

    def do_POST(self):
        if not self._host_ok() or not self._authorised():
            return
        if not self._check_origin():
            return
        if self.path == "/api/printers/add":
            self._handle_add_printer()
        elif self.path == "/api/printers/remove":
            self._handle_remove_printer()
        elif self.path == "/api/printers/update":
            self._handle_update_printer()
        elif self.path == "/api/printers/duplicate":
            self._handle_duplicate_printer()
        elif self.path == "/api/test-connection":
            self._handle_test_connection()
        elif self.path.startswith("/api/print-now/"):
            pid = self.path.split("/")[-1]
            if pid == "all":
                trigger_print_all()
                self._json_response({"ok": True, "message": "Print triggered for all printers"})
            else:
                trigger_print(pid)
                self._json_response({"ok": True, "message": f"Print triggered for {pid}"})
        elif self.path.startswith("/api/queue/") and self.path.endswith("/cancel"):
            self._handle_cancel_queue(self.path.split("/")[-2])
        elif self.path.startswith("/api/toggle-schedule/"):
            pid = self.path.split("/")[-1]
            self._handle_toggle_schedule(pid)
        elif self.path == "/api/upload-image":
            self._handle_upload_image()
        elif self.path == "/api/delete-image":
            self._handle_delete_image()
        elif self.path == "/api/webhook":
            self._handle_webhook_config()
        elif self.path == "/api/notifications":
            self._handle_notifications_config()
        elif self.path == "/api/notifications/test":
            self._handle_notifications_test()
        else:
            self.send_error(404)

    def _find_printer(self, printer_id):
        return next((p for p in read_printers()["printers"] if p["id"] == printer_id), None)

    def _handle_cancel_queue(self, printer_id):
        printer = self._find_printer(printer_id)
        if not printer:
            self._json_response({"ok": False, "message": "Unknown printer"}, 404)
            return
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        job_id = body.get("job_id")
        if job_id is not None and not isinstance(job_id, str):
            self._json_response({"ok": False, "message": "Invalid job_id"}, 400)
            return
        ok = cancel_queue(printer["cups_name"], job_id)
        self._json_response({"ok": ok, "jobs": get_queue(printer["cups_name"])}, 200 if ok else 400)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > MAX_JSON_BYTES:
            raise json.JSONDecodeError("request too large", "", 0)
        body = self.rfile.read(length) if length > 0 else b"{}"
        data = json.loads(body)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("expected a JSON object", "", 0)
        return data

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    # ── Test Connection ──────────────────────────────────────

    def _handle_test_connection(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        ip = str(body.get("ip", "")).strip()
        conn = body.get("connection", "ipp")
        if conn not in ("ipp", "socket") or (ip and not HOST_RE.match(ip)):
            self._json_response({"ok": False, "message": "Invalid IP address or connection"}, 400)
            return
        try:
            port = int(body.get("port", 9100))
            if port < 1 or port > 65535:
                raise ValueError()
        except (ValueError, TypeError):
            self._json_response({"ok": False, "message": "Invalid port number (1-65535)"}, 400)
            return
        if not ip:
            self._json_response({"ok": False, "message": "IP required"}, 400)
            return
        info = probe_printer(ip, conn, port)
        self._json_response({"ok": info["reachable"], **info})

    # ── Printer CRUD ─────────────────────────────────────────

    def _handle_add_printer(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        data = read_printers()
        ip = str(body.get("ip", "")).strip()
        defaults = {"name": f"Printer at {ip}", "connection": "ipp", "port": 9100, "paper_size": "A4",
                    "media_type": "", "test_image": "preset-11",
                    "skip_hours": data["global"].get("skip_hours", 72)}
        fields, err = clean_printer_fields({**defaults, **{k: v for k, v in body.items() if k in defaults or k == "ip"}})
        if err or not fields.get("ip"):
            self._json_response({"ok": False, "message": err or "IP address required"}, 400)
            return
        # The same physical printer may be added more than once (each entry is its
        # own CUPS queue), e.g. one schedule on plain paper and one on photo paper.
        printer_id = f"printer-{uuid.uuid4().hex[:8]}"
        printer = {"id": printer_id, **fields, "paused": False,
                   "cups_name": sanitise_cups_name(f"PBS_{printer_id}")}
        days = clean_int(body.get("interval_days"), 1, 365)
        hour = clean_int(body.get("hour"), 0, 23)
        if days and hour is not None:
            pbs_state.set_interval(printer, days, hour)
        else:
            sched = body.get("schedule") or data["global"].get("schedule", "0 10 */3 * *")
            if not pbs_state.valid_cron(sched):
                self._json_response({"ok": False, "message": "Invalid schedule"}, 400)
                return
            printer["schedule"] = sched
            pbs_state.migrate_printer(printer)
        if not add_cups_printer(printer):
            self._json_response({"ok": False, "message": "Failed to add printer to CUPS. Check the IP."}, 500)
            return
        data["printers"].append(printer)
        write_printers(data)
        update_cron()
        self._json_response({"ok": True, "printer": printer})

    def _handle_remove_printer(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        pid = body.get("id", "")
        data = read_printers()
        printer = next((p for p in data["printers"] if p["id"] == pid), None)
        if not printer:
            self._json_response({"ok": False, "message": "Printer not found"}, 404)
            return
        remove_cups_printer(printer["cups_name"])
        data["printers"] = [p for p in data["printers"] if p["id"] != pid]
        write_printers(data)
        update_cron()
        sf = f"{DATA_DIR}/status-{pid}.json"
        if os.path.exists(sf):
            os.remove(sf)
        self._json_response({"ok": True, "message": f"Printer {printer['name']} removed"})

    def _handle_update_printer(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        pid = body.get("id", "")
        data = read_printers()
        printer = next((p for p in data["printers"] if p["id"] == pid), None)
        if not printer:
            self._json_response({"ok": False, "message": "Printer not found"}, 404)
            return
        fields, err = clean_printer_fields(body)
        if err:
            self._json_response({"ok": False, "message": err}, 400)
            return
        changed_cups = any(k in fields and printer.get(k) != fields[k]
                           for k in ("name", "ip", "connection", "port", "paper_size"))
        printer.update(fields)
        if "interval_days" in body or "hour" in body:
            days = clean_int(body.get("interval_days", printer.get("interval_days", 3)), 1, 365)
            hour = clean_int(body.get("hour", printer.get("hour", 10)), 0, 23)
            if days is None or hour is None:
                self._json_response({"ok": False, "message": "Invalid schedule"}, 400)
                return
            pbs_state.set_interval(printer, days, hour, printer.get("minute", 0))
        elif "schedule" in body:  # raw cron (API/back-compat)
            if not pbs_state.valid_cron(body["schedule"]):
                self._json_response({"ok": False, "message": "Invalid schedule"}, 400)
                return
            for k in ("interval_days", "hour", "minute"):
                printer.pop(k, None)
            printer["schedule"] = str(body["schedule"])
            pbs_state.migrate_printer(printer)
        if changed_cups:
            add_cups_printer(printer)
        write_printers(data)
        update_cron()
        self._json_response({"ok": True, "printer": printer})

    def _handle_duplicate_printer(self):
        """Add another entry for the same physical printer with its own schedule."""
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        data = read_printers()
        src = next((p for p in data["printers"] if p["id"] == body.get("id", "")), None)
        if not src:
            self._json_response({"ok": False, "message": "Printer not found"}, 404)
            return
        printer_id = f"printer-{uuid.uuid4().hex[:8]}"
        printer = dict(src, id=printer_id, cups_name=sanitise_cups_name(f"PBS_{printer_id}"),
                       name=f"{src.get('name', 'Printer')} (2)"[:64], paused=False)
        if not add_cups_printer(printer):
            self._json_response({"ok": False, "message": "Failed to add printer to CUPS."}, 500)
            return
        data["printers"].append(printer)
        write_printers(data)
        update_cron()
        self._json_response({"ok": True, "printer": printer})

    def _handle_toggle_schedule(self, printer_id):
        data = read_printers()
        for p in data["printers"]:
            if p["id"] == printer_id:
                p["paused"] = not p.get("paused", False)
                write_printers(data)
                update_cron()
                state = "paused" if p["paused"] else "running"
                self._json_response({"ok": True, "paused": p["paused"], "message": f"Schedule {state}"})
                return
        self._json_response({"ok": False, "message": "Printer not found"}, 404)

    # ── Webhook config ───────────────────────────────────────

    def _handle_webhook_config(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        data = read_printers()
        if body.get("webhook_url", "").strip() == SECRET_SENTINEL:
            self._json_response({"ok": True})  # unchanged
            return
        if "webhook_url" in body:
            urls = parse_webhook_urls(body["webhook_url"])
            for url in urls:
                err = self._validate_url_safe(url)  # SSRF prevention
                if err:
                    self._json_response({"ok": False, "message": f"Webhook URL {url[:40]}: {err}"}, 400)
                    return
            # Stored in printers.json only; notify.py reads it from there. (It used to be
            # written to /etc/environment too, which cron sources as shell.)
            data.setdefault("global", {})["webhook_url"] = "\n".join(urls)
        write_printers(data)
        self._json_response({"ok": True})

    # ── Notifications (email + HA) ───────────────────────────

    def _validate_url_safe(self, url):
        """Error string if url is unsafe (localhost, link-local/metadata, incl. via DNS), else None."""
        return notify_validate_url(url)

    def _handle_notifications_config(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return

        data = read_printers()
        g = data.setdefault("global", {})

        if "notify_on" in body:
            if body["notify_on"] not in ("all", "problems"):
                self._json_response({"ok": False, "message": "Invalid notify_on"}, 400)
                return
            g["notify_on"] = body["notify_on"]
        if "ink_alert_percent" in body:
            pct = clean_int(body["ink_alert_percent"], 0, 100)
            if pct is None:
                self._json_response({"ok": False, "message": "Ink alert level must be 0-100"}, 400)
                return
            g["ink_alert_percent"] = pct

        # Email config
        if "email" in body:
            ec = body["email"]
            email_cfg = g.get("email", {})
            email_cfg["enabled"] = bool(ec.get("enabled", False))
            email_cfg["smtp_server"] = ec.get("smtp_server", "").strip()
            email_cfg["smtp_port"] = max(1, min(65535, int(ec.get("smtp_port", 587))))
            email_cfg["smtp_from"] = ec.get("smtp_from", "").strip()
            email_cfg["smtp_to"] = ec.get("smtp_to", "").strip()
            email_cfg["smtp_username"] = ec.get("smtp_username", "").strip()
            # Preserve password if sentinel
            pw = ec.get("smtp_password", "")
            if pw != "***":
                email_cfg["smtp_password"] = pw
            email_cfg["smtp_tls"] = bool(ec.get("smtp_tls", True))
            g["email"] = email_cfg

        # Home Assistant config
        if "homeassistant" in body:
            hc = body["homeassistant"]
            ha_cfg = g.get("homeassistant", {})
            ha_url = hc.get("ha_url", "").strip().rstrip("/")
            if ha_url:
                err = self._validate_url_safe(ha_url)
                if err:
                    self._json_response({"ok": False, "message": f"Home Assistant URL: {err}"}, 400)
                    return
            ha_cfg["enabled"] = bool(hc.get("enabled", False))
            ha_cfg["ha_url"] = ha_url
            # Preserve token if sentinel
            token = hc.get("ha_token", "")
            if token != "***":
                ha_cfg["ha_token"] = token
            ha_cfg["ha_verify_ssl"] = bool(hc.get("ha_verify_ssl", True))
            g["homeassistant"] = ha_cfg

        write_printers(data)
        self._json_response({"ok": True})

    def _handle_notifications_test(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        channel = body.get("channel", "")
        if channel not in ("webhook", "email", "homeassistant"):
            self._json_response({"ok": False, "message": "Invalid channel"}, 400)
            return

        # Build config from form values sent in request (test without saving)
        saved = read_printers().get("global", {})
        config = dict(saved)
        if "config" in body:
            fc = body["config"]
            if channel == "webhook":
                url = fc.get("webhook_url", SECRET_SENTINEL)
                if url != SECRET_SENTINEL:
                    config["webhook_url"] = url
            elif channel == "email":
                ec = dict(config.get("email", {}))
                for k in ("smtp_server", "smtp_port", "smtp_from", "smtp_to", "smtp_username", "smtp_tls"):
                    if k in fc:
                        ec[k] = fc[k]
                # Use form password unless it's the sentinel
                pw = fc.get("smtp_password", "")
                if pw and pw != "***":
                    ec["smtp_password"] = pw
                ec["enabled"] = True
                config["email"] = ec
            elif channel == "homeassistant":
                hc = dict(config.get("homeassistant", {}))
                if "ha_url" in fc:
                    hc["ha_url"] = fc["ha_url"]
                token = fc.get("ha_token", "")
                if token and token != "***":
                    hc["ha_token"] = token
                if "ha_verify_ssl" in fc:
                    hc["ha_verify_ssl"] = fc["ha_verify_ssl"]
                hc["enabled"] = True
                config["homeassistant"] = hc

        # Write temp config and run notify.py
        import subprocess, tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"printers": [], "global": config}, tmp)
        tmp.close()
        try:
            result = subprocess.run(
                ["python3", "/app/notify.py", "--event", "test",
                 "--printer", "Test", "--printer-id", "test",
                 "--message", "Test notification", "--channel", channel,
                 "--config", tmp.name],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                self._json_response({"ok": True})
            else:
                err_msg = result.stderr.strip().split("\n")[-1] if result.stderr else "Unknown error"
                err_msg = re.sub(r"^\[[^\]]*\] \[notify\] ", "", err_msg)
                self._json_response({"ok": False, "message": err_msg})
        finally:
            os.unlink(tmp.name)

    # ── Image upload ─────────────────────────────────────────

    def _parse_multipart(self):
        """Parse multipart/form-data without deprecated cgi module."""
        content_type = self.headers.get("Content-Type", "")
        # Extract boundary from Content-Type header
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
                break
        if not boundary:
            return None, None
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_UPLOAD_BYTES + 4096:
            return None, None
        body = self.rfile.read(length)
        boundary_bytes = ("--" + boundary).encode()
        parts = body.split(boundary_bytes)
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            # Split headers from body
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            headers_raw = part[:header_end].decode("utf-8", errors="replace")
            file_data = part[header_end + 4:]
            # Remove trailing \r\n-- if present
            if file_data.endswith(b"\r\n"):
                file_data = file_data[:-2]
            if file_data.endswith(b"--\r\n"):
                file_data = file_data[:-4]
            if file_data.endswith(b"--"):
                file_data = file_data[:-2]
            # Parse filename from Content-Disposition
            fn_match = re.search(r'filename="([^"]+)"', headers_raw)
            if fn_match and b'name="file"' in part[:header_end]:
                return fn_match.group(1), file_data
        return None, None

    def _handle_upload_image(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json_response({"ok": False, "message": "Must be multipart/form-data"}, 400)
            return
        try:
            filename, data = self._parse_multipart()
        except Exception as e:
            self._json_response({"ok": False, "message": str(e)}, 400)
            return
        if not filename or not data:
            self._json_response({"ok": False, "message": "No file uploaded"}, 400)
            return
        filename = os.path.basename(filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ("png", "jpg", "jpeg", "bmp", "tiff"):
            self._json_response({"ok": False, "message": "Invalid file type."}, 400)
            return
        if len(data) > MAX_UPLOAD_BYTES:
            self._json_response({"ok": False, "message": "File too large. Max 5 MB."}, 400)
            return
        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', filename).lstrip(".") or "image"
        if not UPLOAD_NAME_RE.fullmatch(safe_name):
            self._json_response({"ok": False, "message": "Invalid file name."}, 400)
            return
        try:
            from PIL import Image as PILImage
            with PILImage.open(io.BytesIO(data)) as im:
                im.verify()
        except Exception:
            self._json_response({"ok": False, "message": "That file is not a valid image."}, 400)
            return
        with open(os.path.join(UPLOADS_DIR, safe_name), "wb") as f:
            f.write(data)
        self._json_response({"ok": True, "id": f"custom-{safe_name}", "filename": safe_name})

    # ── Image delete ─────────────────────────────────────────

    def _handle_delete_image(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json_response({"ok": False, "message": "Invalid JSON"}, 400)
            return
        image_id = body.get("id", "")
        if not image_id.startswith("custom-"):
            self._json_response({"ok": False, "message": "Can only delete custom images"}, 400)
            return
        filename = image_id.replace("custom-", "", 1)
        # Sanitise — prevent path traversal
        filename = os.path.basename(filename)
        filepath = os.path.join(UPLOADS_DIR, filename)
        if not os.path.exists(filepath):
            self._json_response({"ok": False, "message": "Image not found"}, 404)
            return
        os.remove(filepath)
        # Remove cached thumbnail if it exists
        cache_key = re.sub(r'[^a-zA-Z0-9_-]', '_', filename)
        cache_path = os.path.join(THUMBS_DIR, f"{cache_key}.png")
        if os.path.exists(cache_path):
            os.remove(cache_path)
        # Reset any printers using this image back to default
        data = read_printers()
        for p in data["printers"]:
            if p.get("test_image") == image_id:
                p["test_image"] = "preset-11"
        write_printers(data)
        self._json_response({"ok": True, "message": f"Deleted {filename}"})

    # ── CSV export ───────────────────────────────────────────

    def _serve_history_csv(self):
        history = get_history()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Timestamp", "Result", "Message", "Printer ID"])
        for h in history:
            writer.writerow([h.get("timestamp", ""), h.get("result", ""),
                             h.get("message", ""), h.get("printer_id", "")])
        body = output.getvalue().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition", "attachment; filename=print-history.csv")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    # ── Dashboard HTML ───────────────────────────────────────

    def _serve_dashboard(self):
        data = read_printers()
        printers = data["printers"]
        history = get_history()
        logs = get_recent_logs(25)
        presets = get_available_presets()
        uploads = get_uploaded_images()
        webhook_url = data.get("global", {}).get("webhook_url", "")
        email_cfg = data.get("global", {}).get("email", {})
        ha_cfg = data.get("global", {}).get("homeassistant", {})
        notify_on = data.get("global", {}).get("notify_on", "all")
        ink_alert = int(data.get("global", {}).get("ink_alert_percent", 15))

        printer_cards = ""
        if not printers:
            printer_cards = '<div class="empty-state">No printers configured yet. Add one below or use Discover.</div>'
        else:
            for p in printers:
                st = get_printer_status(p["id"])
                st_colour = {"ok": "var(--good)", "error": "var(--bad)"}.get(st["status"], "var(--muted)")
                st_label = {"ok": "OK", "error": "FAILED"}.get(st["status"], "No prints yet")
                paused = p.get("paused", False)
                pause_label = "Resume" if paused else "Pause"
                pause_class = "btn-good" if paused else "btn-warn"
                sched_state = "PAUSED" if paused else "Active"
                sched_class = "off" if paused else "on"
                test_img = p.get("test_image", "preset-11")
                freq_choices = [(1, "Day"), (2, "2 days"), (3, "3 days"), (4, "4 days"),
                                (5, "5 days"), (7, "Week"), (14, "2 weeks")]
                if pbs_state.is_interval(p):
                    cur_days, cur_hour = p["interval_days"], p.get("hour", 10)
                    if cur_days not in [d for d, _ in freq_choices]:
                        freq_choices.append((cur_days, f"{cur_days} days"))
                    freq_options = "".join(f'<option value="{d}"{" selected" if d == cur_days else ""}>{l}</option>'
                                           for d, l in freq_choices)
                else:  # custom cron expression kept as-is
                    cur_hour = 10
                    freq_options = (f'<option value="" selected>Custom: {html_mod.escape(p.get("schedule", ""))}</option>'
                                    + "".join(f'<option value="{d}">{l}</option>' for d, l in freq_choices))
                thumb = get_image_thumbnail_b64(p)

                img_options = ""
                for pr in presets:
                    sel = "selected" if pr["id"] == test_img else ""
                    img_options += f'<option value="{pr["id"]}" {sel}>{html_mod.escape(pr["label"])}</option>'
                for up in uploads:
                    sel = "selected" if up["id"] == test_img else ""
                    img_options += f'<option value="{up["id"]}" {sel}>{html_mod.escape(up["label"])}</option>'

                thumb_html = f'<img src="{thumb}" class="thumb" alt="test image">' if thumb else ''

                cur_media = p.get("media_type", "")
                media_choices = list(MEDIA_TYPES)
                if cur_media and cur_media not in [k for k, _ in media_choices]:
                    media_choices.append((cur_media, MEDIA_LABELS.get(cur_media, cur_media)))
                media_options = "".join(
                    f'<option value="{html_mod.escape(k)}"{" selected" if k == cur_media else ""}>{html_mod.escape(v)}</option>'
                    for k, v in media_choices)

                printer_cards += f"""
      <div class="card printer-card" data-id="{p["id"]}">
        <div class="printer-header">
          <div class="status-row">
            <div class="conn-dot" id="conn-{p["id"]}" title="Checking connection..."></div>
            <div class="printer-name" id="name-{p["id"]}" ondblclick="startRename('{p["id"]}')"
                 title="Double-click to rename">{html_mod.escape(p["name"])}</div>
            <button class="btn-icon edit" onclick="startRename('{p["id"]}')" title="Rename printer" aria-label="Rename printer"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg></button>
            <span class="printer-ip">{html_mod.escape(p["ip"])}</span>
          </div>
          <div style="display:flex;gap:2px;">
          <button class="btn-icon dup" onclick="duplicatePrinter('{p["id"]}')" title="Add this printer again with its own schedule (e.g. photo paper)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg></button>
          <button class="btn-icon" onclick="removePrinter('{p["id"]}')" title="Remove printer"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14"/></svg></button>
          </div>
        </div>
        <div class="meta" id="model-{p["id"]}"></div>
        <div class="meta" id="status-row-{p["id"]}">
          <span>Status: <strong id="status-label-{p["id"]}" style="color:{st_colour}">{st_label}</strong></span>
          <span>Last: <span id="status-time-{p["id"]}">{html_mod.escape(st["timestamp"])}</span></span>
          <span id="status-msg-{p["id"]}">{html_mod.escape(st["message"])}</span>
        </div>
        <div class="queue-box" id="queue-{p["id"]}" style="display:none;"></div>
        <div class="ink-bar-container" id="ink-{p["id"]}"></div>
        <div class="printer-controls">
          <div class="control-group">
            <label>Print every:</label>
            <select class="input-sm select-sm" id="freq-{p["id"]}"
                    onchange="updateSchedule('{p["id"]}')">
              {freq_options}
            </select>
            <label style="min-width:auto;">at</label>
            <select class="input-sm select-sm" id="hour-{p["id"]}" data-hour="{cur_hour}"
                    onchange="updateSchedule('{p["id"]}')" style="max-width:100px;">
            </select>
            <span class="schedule-state {sched_class}">{sched_state}</span>
            <button class="btn btn-sm {pause_class}" onclick="toggleSchedule('{p["id"]}')">{pause_label}</button>
          </div>
          <div class="control-group">
            <label>Paper size:</label>
            <select class="input-sm select-sm" id="paper-{p["id"]}"
                    onchange="updatePrinter('{p["id"]}', {{paper_size: this.value}})">
              {"".join(f'<option value="{ps}"{" selected" if ps == p.get("paper_size", "A4") else ""}>{ps}</option>' for ps in ["A4", "Letter", "Legal", "A3", "A5", "B5", "4x6", "5x7"])}
            </select>
          </div>
          <div class="control-group">
            <label>Paper type:</label>
            <select class="input-sm select-sm media-select" id="media-{p["id"]}"
                    onchange="updatePrinter('{p["id"]}', {{media_type: this.value}})">
              {media_options}
            </select>
            <span class="meta" title="Epson printers choose the black ink by paper type: plain/matte uses Matte Black, glossy/semi-gloss photo paper uses Photo Black.">\u24d8 sets which black ink is used</span>
          </div>
          <div class="control-group">
            <label>Skip if active within:</label>
            <input type="number" class="input-sm" value="{p.get("skip_hours", 72)}" min="0" max="8760"
                   id="skip-{p["id"]}" onchange="updatePrinter('{p["id"]}', {{skip_hours: parseInt(this.value)}})">
            <span class="meta" title="Skips the maintenance print if the printer finished a job on the same kind of paper (plain vs photo) this recently, including prints from other devices if the printer reports them. 0 = never skip.">hours (0 = never skip) \u24d8</span>
          </div>
          <div class="control-group">
            <label>Test image:</label>
            <select class="input-sm select-sm" id="img-{p["id"]}"
                    onchange="updatePrinter('{p["id"]}', {{test_image: this.value}}); refreshThumb('{p["id"]}')">
              {img_options}
            </select>
            {thumb_html}
            <a href="/api/download-image/{p["id"]}" class="btn btn-xs" title="Download test image"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>Download</a>
          </div>
          <div class="next-print meta" id="next-{p["id"]}">Next print: calculating...</div>
          <div class="btn-row">
            <button class="btn btn-primary btn-sm" onclick="printNow('{p["id"]}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9V2h12v7M6 18H4a2 2 0 01-2-2v-5a2 2 0 012-2h16a2 2 0 012 2v5a2 2 0 01-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>Print Now</button>
            <span class="btn-msg" id="msg-{p["id"]}"></span>
          </div>
        </div>
      </div>"""

        history_json = json.dumps(history[-90:])
        escaped_logs = html_mod.escape(logs)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NozzleWake</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>
  /* Dark cyan theme */
  :root {{
    --bg: #0F1316; --surface: #161B1F; --surface-2: #1E2529; --surface-3: #283136;
    --line: #242C31; --text: #E6EDEF; --muted: #8C9AA1; --faint: #5A676E;
    --accent: #00CDDB; --accent-text: #00191C; --accent-soft: #0D3236;
    --good: #5FD38E; --good-soft: #16352A; --warn: #E8B451; --warn-soft: #3A321F;
    --bad: #F06B6B; --bad-soft: #3C2327; --log: #C3CDD1;
    --radius-card: 12px; --radius-control: 7px;
    --mono: "Cascadia Mono", Consolas, ui-monospace, monospace;
    color-scheme: dark;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
         font-size: 13px; background: var(--bg); color: var(--text); padding: 20px 16px; line-height: 1.5; }}
  .container {{ max-width: 820px; margin: 0 auto; }}
  ::selection {{ background: var(--accent-soft); }}

  /* Scrollbars */
  ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{ background: #34404A; border-radius: 4px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: #43525C; }}
  ::-webkit-scrollbar-thumb:active {{ background: #4F606B; }}
  * {{ scrollbar-width: thin; scrollbar-color: #34404A transparent; }}

  /* Header */
  .app-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }}
  .logo {{ width: 40px; height: 40px; flex-shrink: 0; }}
  h1 {{ font-size: 1.45rem; font-weight: 600; letter-spacing: -0.01em; line-height: 1.2; }}
  .subtitle {{ color: var(--muted); font-size: 0.85rem; }}

  /* Cards */
  .card {{ background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-card);
          padding: 16px 18px; margin-bottom: 14px; }}
  .card h2 {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em;
             color: var(--faint); margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }}

  /* Printer cards */
  .printer-card {{ position: relative; overflow: hidden; }}
  .printer-card::before {{ content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--line); }}
  .printer-card.status-ok::before {{ background: var(--good); }}
  .printer-card.status-error::before {{ background: var(--bad); }}
  .queue-box {{ margin: 8px 0; padding: 8px 10px; border-radius: var(--radius-control); background: var(--warn-soft);
               border: 1px solid rgba(232,180,81,0.25); color: var(--warn); font-size: 0.8rem; }}
  .queue-box.queue-warn {{ background: var(--bad-soft); border-color: rgba(240,107,107,0.3); color: var(--bad); }}
  .queue-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; justify-content: space-between; }}
  .queue-job {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 3px 0;
               border-top: 1px solid rgba(255,255,255,0.06); color: var(--text); }}
  .printer-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }}
  .printer-name {{ font-size: 1.05rem; font-weight: 600; cursor: pointer; border-bottom: 1px dashed transparent; }}
  .printer-name:hover {{ border-bottom-color: var(--faint); }}
  .printer-ip {{ color: var(--faint); font-size: 0.78rem; font-family: var(--mono); margin-left: 8px; }}
  .btn-icon {{ background: none; border: none; color: var(--faint); cursor: pointer;
              padding: 5px 7px; border-radius: 6px; transition: background 0.15s, color 0.15s; display: inline-flex; align-items: center; }}
  .btn-icon:hover {{ background: var(--surface-3); color: var(--bad); }}
  .btn-icon.dup:hover, .btn-icon.edit:hover {{ color: var(--accent); }}
  .btn-icon.edit {{ padding: 3px 5px; }}
  .btn-icon.edit svg {{ width: 14px; height: 14px; }}
  .btn-icon svg {{ width: 16px; height: 16px; }}

  .printer-controls {{ display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }}
  .control-group {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .control-group label {{ font-size: 0.82rem; color: var(--muted); min-width: 140px; }}
  @media (max-width: 600px) {{ .control-group label {{ min-width: 100%; }} }}

  /* Status dot */
  .status-row {{ display: flex; align-items: center; gap: 8px; }}
  .conn-dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--faint); flex-shrink: 0; transition: background 0.3s; }}
  .conn-dot.connected {{ background: var(--good); box-shadow: 0 0 0 3px var(--good-soft); }}
  .conn-dot.disconnected {{ background: var(--bad); box-shadow: 0 0 0 3px var(--bad-soft); }}
  .meta {{ color: var(--muted); font-size: 0.82rem; display: flex; gap: 14px; flex-wrap: wrap; }}
  .schedule-state {{ font-size: 0.75rem; font-weight: 600; padding: 2px 8px; border-radius: 5px; }}
  .schedule-state.on {{ color: var(--good); background: var(--good-soft); }}
  .schedule-state.off {{ color: var(--warn); background: var(--warn-soft); }}

  /* Ink levels */
  .ink-bar-container {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 10px 0 4px; }}
  .ink-bar {{ display: flex; align-items: center; gap: 5px; font-size: 0.72rem; }}
  .ink-bar-bg {{ width: 48px; height: 6px; background: var(--surface-3); border-radius: 3px; overflow: hidden; }}
  .ink-bar-fill {{ height: 100%; border-radius: 3px; transition: width 0.3s;
                  box-shadow: inset 0 0 0 1px rgba(255,255,255,0.22); }}
  .ink-label {{ color: var(--muted); font-variant-numeric: tabular-nums; }}

  .thumb {{ height: 40px; border-radius: 5px; border: 1px solid var(--line); vertical-align: middle; margin-left: 4px; }}

  .next-print {{ font-size: 0.82rem; color: var(--muted); padding: 4px 0; }}
  .next-print strong {{ color: var(--text); font-weight: 600; }}

  /* Buttons: secondary by default */
  .btn {{ color: var(--text); background: var(--surface-2); border: 1px solid var(--line);
         border-radius: var(--radius-control); padding: 9px 22px; font: inherit; font-size: 0.95rem;
         cursor: pointer; font-weight: 600; transition: background 0.15s, border-color 0.15s, filter 0.15s;
         display: inline-flex; align-items: center; gap: 6px; text-decoration: none; }}
  .btn:hover {{ background: var(--surface-3); text-decoration: none; }}
  .btn:active {{ filter: brightness(0.92); }}
  .btn:focus-visible, .btn-icon:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .btn-primary {{ background: var(--accent); border-color: var(--accent); color: var(--accent-text); }}
  .btn-primary:hover {{ background: var(--accent); filter: brightness(1.08); }}
  .btn-warn {{ background: var(--warn-soft); border-color: transparent; color: var(--warn); }}
  .btn-warn:hover {{ background: var(--warn-soft); filter: brightness(1.2); }}
  .btn-good {{ background: var(--good-soft); border-color: transparent; color: var(--good); }}
  .btn-good:hover {{ background: var(--good-soft); filter: brightness(1.2); }}
  .btn-danger {{ background: var(--bad-soft); border-color: transparent; color: var(--bad); }}
  .btn-danger:hover {{ background: var(--bad-soft); filter: brightness(1.2); }}
  .btn-sm {{ padding: 5px 12px; font-size: 0.8rem; }}
  .btn-xs {{ padding: 3px 9px; font-size: 0.75rem; }}
  .btn-row {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 4px; }}
  .btn-msg {{ font-size: 0.8rem; color: var(--muted); }}

  /* Inputs */
  .input-sm, .select-sm, .add-form input, .add-form select {{
    background: var(--surface-2); border: 1px solid var(--line); border-radius: var(--radius-control);
    color: var(--text); padding: 6px 10px; font: inherit; font-size: 0.85rem; transition: border-color 0.15s; }}
  .input-sm:hover, .select-sm:hover, .add-form input:hover, .add-form select:hover {{ border-color: var(--surface-3); }}
  .input-sm:focus, .select-sm:focus, .add-form input:focus, .add-form select:focus {{
    border-color: var(--accent); outline: none; box-shadow: 0 0 0 2px var(--accent-soft); }}
  ::placeholder {{ color: var(--faint); }}
  option {{ background: var(--surface-2); color: var(--text); }}
  input[type=number].input-sm {{ width: 72px; }}
  .select-sm {{ font-size: 0.82rem; max-width: 190px; }}
  .select-sm.media-select {{ max-width: 320px; }}
  select[id^="img-"] {{ max-width: 240px; }}
  input[type=checkbox] {{ accent-color: var(--accent); width: 15px; height: 15px; vertical-align: -2px; margin-right: 4px; }}
  input[type=file] {{ color: var(--muted); font: inherit; font-size: 0.82rem; }}
  input[type=file]::file-selector-button {{ background: var(--surface-2); color: var(--text); border: 1px solid var(--line);
    border-radius: var(--radius-control); padding: 5px 12px; margin-right: 10px; font: inherit; cursor: pointer; }}
  input[type=file]::file-selector-button:hover {{ background: var(--surface-3); }}

  .add-form {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }}
  .add-form .full {{ grid-column: 1 / -1; }}
  .add-form label {{ font-size: 0.8rem; color: var(--muted); margin-bottom: 3px; display: block; }}
  .add-form input, .add-form select {{ width: 100%; padding: 8px 10px; }}
  @media (max-width: 600px) {{ .add-form {{ grid-template-columns: 1fr; }} }}

  .discover-list {{ display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }}
  .discover-item {{ display: flex; justify-content: space-between; align-items: center;
                   background: var(--surface-2); padding: 9px 12px; border-radius: var(--radius-control); }}
  .discover-item:hover {{ background: var(--surface-3); }}

  .empty-state {{ text-align: center; color: var(--faint); padding: 30px 20px; font-size: 0.9rem;
                 background: var(--surface); border: 1px dashed var(--line); border-radius: var(--radius-card); margin-bottom: 14px; }}

  /* Chart */
  .chart-container {{ margin-top: 8px; }}
  .chart-bar-wrap {{ display: flex; align-items: flex-end; gap: 3px; height: 80px; border-bottom: 1px solid var(--line); }}
  .chart-bar {{ flex: 1; min-width: 2px; border-radius: 3px 3px 0 0; cursor: default; position: relative; }}
  .chart-bar:hover {{ filter: brightness(1.15); }}
  .chart-bar[data-tip]:hover::after {{ content: attr(data-tip); position: absolute; bottom: calc(100% + 6px);
    left: 50%; transform: translateX(-50%); background: var(--surface-3); color: var(--text);
    padding: 4px 8px; border-radius: 6px; font-size: 0.72rem; white-space: nowrap; z-index: 10; pointer-events: none; }}
  .chart-labels {{ display: flex; justify-content: space-between; font-size: 0.7rem; color: var(--faint); margin-top: 4px; padding: 0 2px; }}
  .chart-legend {{ display: flex; gap: 16px; font-size: 0.75rem; color: var(--muted); margin-top: 10px; }}
  .chart-legend span::before {{ content: ''; display: inline-block; width: 8px; height: 8px;
                               border-radius: 2px; margin-right: 5px; vertical-align: middle; }}
  .legend-ok::before {{ background: var(--good); }}
  .legend-skip::before {{ background: var(--warn); }}
  .legend-err::before {{ background: var(--bad); }}

  .upload-area {{ display: flex; align-items: center; gap: 12px; margin-top: 10px; flex-wrap: wrap; }}
  .uploaded-item {{ display: flex; align-items: center; justify-content: space-between;
                   background: var(--surface-2); padding: 5px 8px 5px 12px; border-radius: var(--radius-control); margin-top: 6px; }}

  /* Notification tabs */
  .notif-tabs {{ display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin: 14px 0 14px; }}
  .notif-tab {{ padding: 8px 14px; cursor: pointer; color: var(--muted); font-size: 0.85rem; font-weight: 600;
               border-bottom: 2px solid transparent; margin-bottom: -1px; transition: color 0.15s; user-select: none; }}
  .notif-tab:hover {{ color: var(--text); }}
  .notif-tab.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
  .notif-panel {{ display: none; }}
  .notif-panel.active {{ display: block; }}
  .notif-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }}
  .notif-row label {{ min-width: 110px; color: var(--muted); font-size: 0.82rem; }}
  .notif-row input[type="text"], .notif-row input[type="url"], .notif-row input[type="email"],
  .notif-row input[type="password"], .notif-row input[type="number"] {{ flex: 1; min-width: 180px; }}
  .webhook-row {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .webhook-row input, .webhook-row textarea {{ flex: 1; min-width: 200px; }}
  textarea.input-sm {{ font-family: var(--mono); font-size: 0.78rem; resize: vertical; }}

  /* Toasts */
  .toast-container {{ position: fixed; bottom: 20px; right: 20px; z-index: 1000; display: flex;
                     flex-direction: column; gap: 8px; pointer-events: none; }}
  .toast {{ background: var(--surface-3); border: 1px solid var(--line); border-radius: 10px; padding: 11px 15px;
           font-size: 0.85rem; color: var(--text); box-shadow: 0 8px 24px rgba(0,0,0,0.35);
           border-left: 3px solid var(--accent); pointer-events: auto; animation: toast-in 0.25s ease-out; max-width: 340px; }}
  .toast.success {{ border-left-color: var(--good); }}
  .toast.error {{ border-left-color: var(--bad); }}
  .toast.fade-out {{ animation: toast-out 0.3s ease-in forwards; }}
  @keyframes toast-in {{ from {{ transform: translateY(8px); opacity: 0; }} to {{ transform: none; opacity: 1; }} }}
  @keyframes toast-out {{ from {{ opacity: 1; }} to {{ opacity: 0; transform: translateY(8px); }} }}

  pre {{ background: var(--bg); border: 1px solid var(--line); border-radius: var(--radius-control); padding: 12px;
        font-family: var(--mono); font-size: 0.76rem; overflow-x: auto; max-height: 280px; overflow-y: auto;
        color: var(--log); line-height: 1.65; white-space: pre-wrap; word-break: break-all; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .footer {{ text-align: center; color: var(--faint); font-size: 0.75rem; margin-top: 20px; }}
  code {{ background: var(--surface-2); padding: 2px 6px; border-radius: 5px; font-family: var(--mono); font-size: 0.82rem; }}
  .section-divider {{ border-top: 1px solid var(--line); margin: 14px 0; }}

  .rename-input {{ background: var(--surface-2); border: 1px solid var(--accent); border-radius: 6px;
                  color: var(--text); padding: 3px 8px; font: inherit; font-size: 1rem; font-weight: 600; width: 220px; outline: none; }}
</style>
</head>
<body>
<div class="container">
  <div class="app-header">
    <svg class="logo" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="40" height="40" aria-hidden="true"> <defs> <linearGradient id="nwTile" x1="0" y1="0" x2="1" y2="1"> <stop offset="0" stop-color="#3DE7F1"/> <stop offset="1" stop-color="#009FB2"/> </linearGradient> </defs> <rect width="512" height="512" rx="112" fill="url(#nwTile)"/> <rect x="112" y="96" width="288" height="116" rx="30" fill="#00191C"/> <rect x="166" y="138" width="44" height="32" rx="10" fill="#3DE7F1"/> <rect x="234" y="138" width="44" height="32" rx="10" fill="#FF5FB8"/> <rect x="302" y="138" width="44" height="32" rx="10" fill="#FFD84A"/> <path d="M210 212 H302 L284 254 H228 Z" fill="#00191C"/> <path d="M256 272 C 256 272 186 352 186 390 A 70 70 0 0 0 326 390 C 326 352 256 272 256 272 Z" fill="#00191C"/> <path d="M226 396 A 32 32 0 0 0 250 426" fill="none" stroke="#3DE7F1" stroke-width="12" stroke-linecap="round"/> </svg>
    <div>
      <h1>NozzleWake</h1>
      <p class="subtitle">Automated Print Head Maintenance</p>
    </div>
  </div>

  <div id="printerCards">{printer_cards}</div>

  <!-- Add Printer -->
  <div class="card">
    <h2>Add Printer</h2>
    <div class="btn-row" style="margin-bottom:10px;">
      <button class="btn btn-sm" onclick="discoverPrinters()" id="discoverBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>Discover on Network</button>
      <span class="btn-msg" id="discoverMsg"></span>
    </div>
    <div id="discoverResults"></div>
    <div class="section-divider"></div>
    <div class="add-form" id="addForm">
      <div>
        <label for="addName">Name</label>
        <input type="text" id="addName" placeholder="e.g. Canon Pro 1100" maxlength="64">
      </div>
      <div>
        <label for="addIp">IP Address *</label>
        <div style="display:flex;gap:6px;">
          <input type="text" id="addIp" placeholder="192.168.1.50" required style="flex:1;">
          <button class="btn btn-sm" style="white-space:nowrap;" onclick="testConnection()">Test</button>
        </div>
        <span class="btn-msg" id="testMsg" style="margin-top:4px;display:block;"></span>
      </div>
      <div>
        <label for="addConn">Connection</label>
        <select id="addConn">
          <option value="ipp" selected>IPP (recommended)</option>
          <option value="socket">Socket (port 9100)</option>
        </select>
      </div>
      <div>
        <label for="addPaper">Paper Size</label>
        <input type="text" id="addPaper" value="A4" maxlength="20">
      </div>
      <div>
        <label for="addMedia">Paper type</label>
        <select id="addMedia">{"".join(f'<option value="{k}">{html_mod.escape(v)}</option>' for k, v in MEDIA_TYPES)}</select>
      </div>
      <div>
        <label>&nbsp;</label>
        <span class="meta">The same printer can be added more than once \u2014 e.g. plain paper (Matte Black) and photo paper (Photo Black) on separate schedules.</span>
      </div>
      <div>
        <label for="addFreq">Print every</label>
        <select id="addFreq">
          <option value="1">Day</option>
          <option value="2">2 days</option>
          <option value="3" selected>3 days</option>
          <option value="4">4 days</option>
          <option value="5">5 days</option>
          <option value="7">Week</option>
          <option value="14">2 weeks</option>
        </select>
      </div>
      <div>
        <label for="addHour">At</label>
        <select id="addHour"></select>
      </div>
      <div class="full">
        <button class="btn btn-primary" onclick="addPrinter()" id="addBtn">Add Printer</button>
        <span class="btn-msg" id="addMsg" style="margin-left:10px;"></span>
      </div>
    </div>
  </div>

  <!-- Custom Test Image -->
  <div class="card">
    <h2>Custom Test Image</h2>
    <p class="meta" style="margin-bottom:8px;">Upload a custom test image (PNG, JPG, BMP, TIFF \u2014 max 5 MB).</p>
    <div class="upload-area">
      <input type="file" id="uploadFile" accept=".png,.jpg,.jpeg,.bmp,.tiff">
      <button class="btn btn-primary btn-sm" onclick="uploadImage()">Upload</button>
      <span class="btn-msg" id="uploadMsg"></span>
    </div>
    <div id="uploadedList" style="margin-top:10px;">{"".join(f'<div class="uploaded-item" id="upl-{html_mod.escape(u["id"])}"><span class="meta">{html_mod.escape(u["label"])}</span><button class="btn-icon" onclick="deleteImage(&#39;{html_mod.escape(u["id"])}&#39;)" title="Delete"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14"/></svg></button></div>' for u in uploads) if uploads else ""}</div>
  </div>

  <!-- Notifications (tabbed) -->
  <div class="card">
    <h2>Notifications</h2>
    <div class="notif-row">
      <label for="notifyOn">Notify on</label>
      <select class="input-sm select-sm" id="notifyOn" style="max-width:420px;flex:1;">
        <option value="all"{"" if notify_on == "problems" else " selected"}>Everything (incl. successful prints)</option>
        <option value="problems"{" selected" if notify_on == "problems" else ""}>Problems only (failures, offline, recovered, low ink)</option>
      </select>
    </div>
    <div class="notif-row">
      <label for="inkAlert">Low ink alert</label>
      <input type="number" class="input-sm" id="inkAlert" value="{ink_alert}" min="0" max="100" style="width:72px;min-width:0;flex:none;">
      <span class="meta">% or below, once per ink until refilled (0 = off)</span>
      <button class="btn btn-primary btn-sm" onclick="saveGeneralNotif()">Save</button>
    </div>
    <div class="notif-tabs">
      <div class="notif-tab active" data-tab="webhook" onclick="switchNotifTab('webhook')">Webhook</div>
      <div class="notif-tab" data-tab="email" onclick="switchNotifTab('email')">Email</div>
      <div class="notif-tab" data-tab="ha" onclick="switchNotifTab('ha')">Home Assistant</div>
    </div>

    <!-- Webhook tab -->
    <div class="notif-panel active" id="notif-webhook">
      <p class="meta" style="margin-bottom:8px;">HTTP POST notifications. Discord, Slack and ntfy.sh URLs are formatted automatically; anything else gets JSON. One URL per line to send to several.</p>
      <div class="webhook-row">
        <textarea class="input-sm" id="webhookUrl" rows="3"
               placeholder="https://discord.com/api/webhooks/...&#10;https://hooks.slack.com/services/...">{SECRET_SENTINEL if webhook_url else ""}</textarea>
      </div>
      <p class="meta" style="margin:4px 0 8px;">Saved URLs show as *** \u2014 to change them, clear the box and enter all URLs again.</p>
      <div class="webhook-row">
        <button class="btn btn-primary btn-sm" onclick="saveWebhook()">Save</button>
        <button class="btn btn-sm" onclick="testNotification('webhook')">Test</button>
        <span class="btn-msg" id="webhookMsg"></span>
        <span class="btn-msg" id="webhookTestMsg"></span>
      </div>
    </div>

    <!-- Email tab -->
    <div class="notif-panel" id="notif-email">
      <p class="meta" style="margin-bottom:8px;">Send email notifications via SMTP on print success/failure.</p>
      <div class="notif-row">
        <label><input type="checkbox" id="emailEnabled" {"checked" if email_cfg.get("enabled") else ""}> Enabled</label>
      </div>
      <div class="notif-row">
        <label>SMTP Server</label>
        <input type="text" class="input-sm" id="smtpServer" value="{html_mod.escape(str(email_cfg.get("smtp_server", "")))}" placeholder="smtp.gmail.com">
        <label style="min-width:auto;">Port</label>
        <input type="number" class="input-sm" id="smtpPort" value="{email_cfg.get("smtp_port", 587)}" style="width:80px;min-width:0;flex:none;" min="1" max="65535">
      </div>
      <div class="notif-row">
        <label>From</label>
        <input type="email" class="input-sm" id="smtpFrom" value="{html_mod.escape(str(email_cfg.get("smtp_from", "")))}" placeholder="printer@example.com">
      </div>
      <div class="notif-row">
        <label>To</label>
        <input type="email" class="input-sm" id="smtpTo" value="{html_mod.escape(str(email_cfg.get("smtp_to", "")))}" placeholder="you@example.com">
      </div>
      <div class="notif-row">
        <label>Username</label>
        <input type="text" class="input-sm" id="smtpUser" value="{html_mod.escape(str(email_cfg.get("smtp_username", "")))}" placeholder="(optional)">
      </div>
      <div class="notif-row">
        <label>Password</label>
        <input type="password" class="input-sm" id="smtpPass" value="{"***" if email_cfg.get("smtp_password") else ""}" placeholder="(optional)">
      </div>
      <div class="notif-row">
        <label><input type="checkbox" id="smtpTls" {"checked" if email_cfg.get("smtp_tls", True) else ""}> Use TLS (STARTTLS)</label>
      </div>
      <div class="notif-row" style="margin-top:8px;">
        <button class="btn btn-primary btn-sm" onclick="saveEmailConfig()">Save</button>
        <button class="btn btn-sm" onclick="testNotification('email')">Test</button>
        <span class="btn-msg" id="emailMsg"></span>
        <span class="btn-msg" id="emailTestMsg"></span>
      </div>
    </div>

    <!-- Home Assistant tab -->
    <div class="notif-panel" id="notif-ha">
      <p class="meta" style="margin-bottom:8px;">Creates a persistent notification in Home Assistant.</p>
      <div class="notif-row">
        <label><input type="checkbox" id="haEnabled" {"checked" if ha_cfg.get("enabled") else ""}> Enabled</label>
      </div>
      <div class="notif-row">
        <label>HA URL</label>
        <input type="url" class="input-sm" id="haUrl" value="{html_mod.escape(str(ha_cfg.get("ha_url", "")))}" placeholder="http://homeassistant.local:8123">
      </div>
      <div class="notif-row">
        <label>Access Token</label>
        <input type="password" class="input-sm" id="haToken" value="{"***" if ha_cfg.get("ha_token") else ""}" placeholder="Long-lived access token">
      </div>
      <div class="notif-row">
        <label><input type="checkbox" id="haVerifySsl" {"checked" if ha_cfg.get("ha_verify_ssl", True) else ""}> Verify SSL</label>
      </div>
      <div class="notif-row" style="margin-top:8px;">
        <button class="btn btn-primary btn-sm" onclick="saveHAConfig()">Save</button>
        <button class="btn btn-sm" onclick="testNotification('homeassistant')">Test</button>
        <span class="btn-msg" id="haMsg"></span>
        <span class="btn-msg" id="homeassistantTestMsg"></span>
      </div>
    </div>
  </div>

  <!-- Print History -->
  <div class="card">
    <h2>Print History (Last 30 days)
      <a href="/api/history.csv" style="font-size:0.7rem;text-transform:none;letter-spacing:0;margin-left:10px;font-weight:400;">Export CSV</a>
    </h2>
    <div class="chart-container">
      <div class="chart-bar-wrap" id="chartBars"></div>
      <div class="chart-labels" id="chartLabels"></div>
      <div class="chart-legend">
        <span class="legend-ok">Printed</span>
        <span class="legend-skip">Skipped</span>
        <span class="legend-err">Failed</span>
      </div>
    </div>
  </div>

  <!-- Logs -->
  <div class="card">
    <h2>Recent Logs</h2>
    <pre id="logs">{escaped_logs}</pre>
  </div>

  <div class="card" style="padding: 12px 16px;">
    <a href="/printers" target="_blank">CUPS Printer Status (port 631)</a>
  </div>

  <div class="footer">NozzleWake v2.3.0</div>
</div>
<div class="toast-container" id="toastContainer"></div>

<script>
const historyData = {history_json};

function esc(s) {{ if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }}

function api(path, method, body) {{
  const opts = {{ method, headers: {{ 'X-NozzleWake': '1' }} }};
  if (body) {{ opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }}
  return fetch(path, opts).then(r => r.json());
}}
function setMsg(id, text, timeout) {{
  const el = document.getElementById(id);
  if (el) {{ el.textContent = text; if (timeout) setTimeout(() => el.textContent = '', timeout); }}
}}

function showToast(message, type) {{
  const c = document.getElementById('toastContainer');
  const t = document.createElement('div');
  t.className = 'toast ' + (type || '');
  t.textContent = message;
  c.appendChild(t);
  setTimeout(() => {{ t.classList.add('fade-out'); setTimeout(() => t.remove(), 300); }}, 3000);
}}

// ── Printer actions ─────────────────────────────────────
function printNow(id) {{
  api('/api/print-now/' + id, 'POST').then(() => {{
    showToast('Print triggered', 'success');
    setTimeout(refreshLogs, 3000);
    setTimeout(refreshAllStatus, 5000);
    setTimeout(refreshAllStatus, 15000);
  }});
}}
function toggleSchedule(id) {{
  api('/api/toggle-schedule/' + id, 'POST').then(() => location.reload());
}}
function updatePrinter(id, updates) {{
  updates.id = id;
  api('/api/printers/update', 'POST', updates).then(d => {{
    if (d.ok) showToast('Settings saved', 'success');
  }});
}}
function duplicatePrinter(id) {{
  if (!confirm('Add this printer again with its own schedule and settings?')) return;
  api('/api/printers/duplicate', 'POST', {{ id }}).then(d => {{
    if (d.ok) location.reload();
    else showToast(d.message || 'Could not duplicate printer', 'error');
  }});
}}
const MEDIA_LABELS = {json.dumps(MEDIA_LABELS)};
function applyMediaTypes(id, supported) {{
  const sel = document.getElementById('media-' + id);
  if (!sel || !supported || !supported.length || sel.dataset.probed) return;
  sel.dataset.probed = '1';
  const cur = sel.value;
  const keys = [''].concat(supported.filter(k => k));
  if (cur && !keys.includes(cur)) keys.push(cur);
  sel.replaceChildren();
  keys.forEach(k => {{
    const o = document.createElement('option');
    o.value = k; o.textContent = MEDIA_LABELS[k] || k; if (k === cur) o.selected = true;
    sel.appendChild(o);
  }});
}}
function removePrinter(id) {{
  if (!confirm('Remove this printer?')) return;
  api('/api/printers/remove', 'POST', {{ id }}).then(() => location.reload());
}}

// ── Rename ──────────────────────────────────────────────
function startRename(id) {{
  const el = document.getElementById('name-' + id);
  if (el.querySelector('input')) return;
  const current = el.textContent;
  const input = document.createElement('input');
  input.className = 'rename-input';
  input.value = current;
  input.defaultValue = current;
  input.addEventListener('blur', () => finishRename(id, input));
  input.addEventListener('keydown', e => {{
    if (e.key === 'Enter') input.blur();
    if (e.key === 'Escape') {{ input.value = current; input.blur(); }}
  }});
  el.replaceChildren(input);
  input.focus();
  input.select();
}}
function finishRename(id, input) {{
  const name = input.value.trim();
  if (name && name !== input.defaultValue) {{
    updatePrinter(id, {{ name }});
  }}
  const el = document.getElementById('name-' + id);
  el.textContent = name || input.defaultValue;
}}

// ── Test Connection ─────────────────────────────────────
function testConnection() {{
  const ip = document.getElementById('addIp').value.trim();
  if (!ip) {{ setMsg('testMsg', 'Enter an IP first', 3000); return; }}
  setMsg('testMsg', 'Testing...');
  api('/api/test-connection', 'POST', {{ ip, connection: document.getElementById('addConn').value }})
    .then(d => {{
      if (d.reachable) {{
        let msg = 'Connected!';
        if (d.model) {{
          msg += ' Model: ' + d.model;
          document.getElementById('addName').value = d.model;
        }}
        setMsg('testMsg', msg, 8000);
      }} else {{
        setMsg('testMsg', 'Cannot reach printer: ' + (d.error || 'timeout'), 8000);
      }}
    }}).catch(() => setMsg('testMsg', 'Test failed', 3000));
}}

// ── Connection status + ink levels (probe all printers) ─
function probeAllPrinters() {{
  document.querySelectorAll('.printer-card').forEach(card => {{
    const id = card.dataset.id;
    // Get IP from the card
    const ipEl = card.querySelector('.printer-ip');
    if (!ipEl) return;
    const ip = ipEl.textContent.trim();
    api('/api/probe/' + ip, 'GET').then(d => {{
      // Connection dot
      const dot = document.getElementById('conn-' + id);
      if (dot) {{
        dot.className = 'conn-dot ' + (d.reachable ? 'connected' : 'disconnected');
        dot.title = d.reachable ? 'Connected' : 'Unreachable';
      }}
      // Update card border color based on status
      const card = dot ? dot.closest('.printer-card') : null;
      if (card) {{
        card.classList.remove('status-ok', 'status-error');
        card.classList.add(d.reachable ? 'status-ok' : 'status-error');
      }}
      applyMediaTypes(id, d.media_types);
      // Model name
      const modelEl = document.getElementById('model-' + id);
      if (modelEl && d.model) {{
        modelEl.innerHTML = '<span style="color:var(--faint);font-size:0.78rem;">' + esc(d.model) + '</span>';
      }}
      // Ink levels
      const inkEl = document.getElementById('ink-' + id);
      if (inkEl && d.ink_levels && d.ink_levels.length > 0) {{
        let html = '';
        d.ink_levels.forEach(ink => {{
          const pct = ink.level >= 0 ? ink.level : 0;
          const color = ink.color || '#8C9AA1';
          // Convert IPP color format (#RRGGBB) or use default
          const barColor = color.startsWith('#') ? color : '#8C9AA1';
          html += `<div class="ink-bar">
            <span class="ink-label">${{esc(ink.name)}}</span>
            <div class="ink-bar-bg"><div class="ink-bar-fill" style="width:${{pct}}%;background:${{barColor}};"></div></div>
            <span class="ink-label">${{pct >= 0 ? pct + '%' : '?'}}</span>
          </div>`;
        }});
        inkEl.innerHTML = html;
      }}
    }}).catch(() => {{}});
  }});
}}

// ── Schedule helpers ─────────────────────────────────────
function populateHourSelect(sel, selected) {{
  sel.innerHTML = '';
  for (let h = 0; h < 24; h++) {{
    const l = h === 0 ? '12:00 AM' : h < 12 ? h+':00 AM' : h === 12 ? '12:00 PM' : (h-12)+':00 PM';
    const o = document.createElement('option');
    o.value = h; o.textContent = l; if (h === selected) o.selected = true;
    sel.appendChild(o);
  }}
}}
function initSchedulePickers() {{
  document.querySelectorAll('[id^="hour-"]').forEach(sel => populateHourSelect(sel, parseInt(sel.dataset.hour)));
  const addHour = document.getElementById('addHour');
  if (addHour) populateHourSelect(addHour, 10);
}}
function updateSchedule(id) {{
  const days = parseInt(document.getElementById('freq-' + id).value);
  const hour = parseInt(document.getElementById('hour-' + id).value);
  if (!days) return;
  updatePrinter(id, {{ interval_days: days, hour }});
  setTimeout(() => refreshNextPrint(), 500);
}}

function addPrinter() {{
  const ip = document.getElementById('addIp').value.trim();
  if (!ip) {{ showToast('IP address is required', 'error'); return; }}
  const btn = document.getElementById('addBtn');
  btn.disabled = true;
  api('/api/printers/add', 'POST', {{
    name: document.getElementById('addName').value.trim() || ('Printer at ' + ip),
    ip, connection: document.getElementById('addConn').value,
    paper_size: document.getElementById('addPaper').value.trim() || 'A4',
    media_type: document.getElementById('addMedia').value,
    interval_days: parseInt(document.getElementById('addFreq').value),
    hour: parseInt(document.getElementById('addHour').value)
  }}).then(d => {{
    btn.disabled = false;
    if (d.ok) location.reload();
    else showToast(d.message || 'Failed to add printer', 'error');
  }}).catch(() => {{ btn.disabled = false; showToast('Network error', 'error'); }});
}}

// ── Discovery ───────────────────────────────────────────
function discoverPrinters() {{
  const btn = document.getElementById('discoverBtn');
  const container = document.getElementById('discoverResults');
  btn.disabled = true; btn.textContent = 'Scanning...';
  api('/api/discover', 'GET').then(d => {{
    btn.disabled = false; btn.textContent = 'Discover on Network';
    if (!d.printers || !d.printers.length) {{
      showToast('No printers found. Try adding manually.', '');
      container.innerHTML = ''; return;
    }}
    // Names come from devices on the network: build with textContent, never HTML.
    const list = document.createElement('div');
    list.className = 'discover-list';
    d.printers.forEach(p => {{
      const item = document.createElement('div');
      item.className = 'discover-item';
      const info = document.createElement('div');
      const name = document.createElement('strong');
      name.textContent = p.name;
      const ip = document.createElement('span');
      ip.className = 'meta';
      ip.textContent = ' ' + p.ip;
      info.append(name, ip);
      const btn = document.createElement('button');
      btn.className = 'btn btn-primary btn-sm';
      btn.textContent = 'Add';
      btn.addEventListener('click', () => quickAdd(String(p.ip), String(p.name)));
      item.append(info, btn);
      list.append(item);
    }});
    container.replaceChildren(list);
  }}).catch(() => {{ btn.disabled = false; btn.textContent = 'Discover on Network'; }});
}}
function quickAdd(ip, name) {{
  document.getElementById('addIp').value = ip;
  document.getElementById('addName').value = name;
  addPrinter();
}}

// ── Image upload ────────────────────────────────────────
function uploadImage() {{
  const input = document.getElementById('uploadFile');
  if (!input.files.length) {{ showToast('Select a file first', 'error'); return; }}
  const file = input.files[0];
  if (file.size > 5*1024*1024) {{ showToast('Too large (max 5 MB)', 'error'); return; }}
  const form = new FormData(); form.append('file', file);
  fetch('/api/upload-image', {{ method: 'POST', body: form, headers: {{ 'X-NozzleWake': '1' }} }}).then(r => r.json()).then(d => {{
    if (d.ok) {{ showToast('Image uploaded!', 'success'); setTimeout(() => location.reload(), 1500); }}
    else showToast(d.message || 'Upload failed', 'error');
  }}).catch(e => showToast('Upload error', 'error'));
}}

function deleteImage(imageId) {{
  if (!confirm('Delete this image? Printers using it will revert to the default.')) return;
  api('/api/delete-image', 'POST', {{ id: imageId }}).then(d => {{
    if (d.ok) {{
      const el = document.getElementById('upl-' + imageId);
      if (el) el.remove();
      // Remove from all dropdowns and refresh thumbnails for affected printers
      document.querySelectorAll(`option[value="${{imageId}}"]`).forEach(o => {{
        const sel = o.parentElement;
        const wasSelected = o.selected;
        o.remove();
        if (wasSelected) {{
          // Reset dropdown to preset-11 and refresh thumbnail
          sel.value = 'preset-11';
          const id = sel.id.replace('img-', '');
          refreshThumb(id);
        }}
      }});
    }} else {{
      alert(d.message || 'Delete failed');
    }}
  }});
}}

function refreshThumb(id) {{
  // Delay to let server save the updated test_image first
  setTimeout(() => {{
    api('/api/thumbnail/' + id, 'GET').then(d => {{
      const card = document.querySelector(`[data-id="${{id}}"]`);
      if (!card || !d.thumbnail) return;
      let img = card.querySelector('.thumb');
      if (img) {{
        img.src = d.thumbnail;
      }} else {{
        // Create thumbnail if it didn't exist before
        const sel = card.querySelector('[id^="img-"]');
        if (sel) {{
          img = document.createElement('img');
          img.className = 'thumb';
          img.alt = 'test image';
          img.src = d.thumbnail;
          sel.parentNode.insertBefore(img, sel.nextSibling);
        }}
      }}
    }});
  }}, 500);
}}

// ── Notifications ──────────────────────────────────────
function switchNotifTab(tab) {{
  document.querySelectorAll('.notif-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.notif-panel').forEach(p => p.classList.toggle('active', p.id === 'notif-' + tab));
}}

function saveWebhook() {{
  const url = document.getElementById('webhookUrl').value.trim();
  api('/api/webhook', 'POST', {{ webhook_url: url }}).then(d => {{
    showToast(d.ok ? 'Webhook saved' : (d.message || 'Error saving webhook'), d.ok ? 'success' : 'error');
  }});
}}

function saveGeneralNotif() {{
  api('/api/notifications', 'POST', {{
    notify_on: document.getElementById('notifyOn').value,
    ink_alert_percent: parseInt(document.getElementById('inkAlert').value) || 0
  }}).then(d => showToast(d.ok ? 'Notification settings saved' : (d.message || 'Error'), d.ok ? 'success' : 'error'));
}}

function saveEmailConfig() {{
  const cfg = {{
    email: {{
      enabled: document.getElementById('emailEnabled').checked,
      smtp_server: document.getElementById('smtpServer').value.trim(),
      smtp_port: parseInt(document.getElementById('smtpPort').value) || 587,
      smtp_from: document.getElementById('smtpFrom').value.trim(),
      smtp_to: document.getElementById('smtpTo').value.trim(),
      smtp_username: document.getElementById('smtpUser').value.trim(),
      smtp_password: document.getElementById('smtpPass').value,
      smtp_tls: document.getElementById('smtpTls').checked
    }}
  }};
  api('/api/notifications', 'POST', cfg).then(d => {{
    showToast(d.ok ? 'Email settings saved' : (d.message || 'Error'), d.ok ? 'success' : 'error');
  }});
}}

function saveHAConfig() {{
  const cfg = {{
    homeassistant: {{
      enabled: document.getElementById('haEnabled').checked,
      ha_url: document.getElementById('haUrl').value.trim(),
      ha_token: document.getElementById('haToken').value,
      ha_verify_ssl: document.getElementById('haVerifySsl').checked
    }}
  }};
  api('/api/notifications', 'POST', cfg).then(d => {{
    showToast(d.ok ? 'Home Assistant settings saved' : (d.message || 'Error'), d.ok ? 'success' : 'error');
  }});
}}

function testNotification(channel) {{
  let cfg = {{}};
  if (channel === 'webhook') {{
    cfg.webhook_url = document.getElementById('webhookUrl').value.trim();
  }} else if (channel === 'email') {{
    cfg = {{
      smtp_server: document.getElementById('smtpServer').value.trim(),
      smtp_port: parseInt(document.getElementById('smtpPort').value) || 587,
      smtp_from: document.getElementById('smtpFrom').value.trim(),
      smtp_to: document.getElementById('smtpTo').value.trim(),
      smtp_username: document.getElementById('smtpUser').value.trim(),
      smtp_password: document.getElementById('smtpPass').value,
      smtp_tls: document.getElementById('smtpTls').checked
    }};
  }} else if (channel === 'homeassistant') {{
    cfg = {{
      ha_url: document.getElementById('haUrl').value.trim(),
      ha_token: document.getElementById('haToken').value,
      ha_verify_ssl: document.getElementById('haVerifySsl').checked
    }};
  }}
  const names = {{ webhook: 'Webhook', email: 'Email', homeassistant: 'Home Assistant' }};
  showToast('Sending ' + names[channel] + ' test...', '');
  api('/api/notifications/test', 'POST', {{ channel: channel, config: cfg }}).then(d => {{
    showToast(d.ok ? names[channel] + ' test sent!' : (d.message || 'Test failed'), d.ok ? 'success' : 'error');
  }});
}}

// ── History chart ───────────────────────────────────────
function renderChart() {{
  const container = document.getElementById('chartBars');
  const labels = document.getElementById('chartLabels');
  if (!historyData.length) {{
    container.innerHTML = '<span style="color:var(--faint);font-size:0.8rem;padding:30px 0;display:block;text-align:center;width:100%;">No print history yet</span>';
    return;
  }}
  const byDate = {{}};
  historyData.forEach(e => {{ const d = e.timestamp.split(' ')[0]; if (!byDate[d]) byDate[d] = []; byDate[d].push(e); }});
  const days = []; const now = new Date();
  const localDate = d => `${{d.getFullYear()}}-${{String(d.getMonth()+1).padStart(2,'0')}}-${{String(d.getDate()).padStart(2,'0')}}`;
  for (let i = 29; i >= 0; i--) {{ const d = new Date(now); d.setDate(d.getDate()-i); const k = localDate(d); days.push({{ date: k, entries: byDate[k] || [] }}); }}
  container.innerHTML = '';
  days.forEach(day => {{
    const bar = document.createElement('div'); bar.className = 'chart-bar';
    const oks = day.entries.filter(e => e.result === 'ok').length;
    const skips = day.entries.filter(e => e.result === 'skipped').length;
    const errs = day.entries.filter(e => e.result === 'error').length;
    const total = oks + skips + errs;
    if (!total) {{ bar.style.height = '4px'; bar.style.background = 'var(--surface-3)'; }}
    else {{ bar.style.height = Math.max(8, Math.min(100, total*20))+'px'; bar.style.background = errs > 0 ? 'var(--bad)' : oks > 0 ? 'var(--good)' : 'var(--warn)'; }}
    bar.title = `${{day.date}}: ${{oks}} printed, ${{skips}} skipped, ${{errs}} failed`;
    if (total) bar.setAttribute('data-tip', `${{day.date.slice(5)}}: ${{oks}}ok ${{skips}}skip ${{errs}}err`);
    container.appendChild(bar);
  }});
  if (days.length >= 3) {{
    const f = d => d.slice(5);
    labels.innerHTML = `<span>${{f(days[0].date)}}</span><span>${{f(days[14].date)}}</span><span>${{f(days[29].date)}}</span>`;
  }}
}}

function refreshLogs() {{
  fetch('/api/logs').then(r => r.json()).then(d => {{
    const el = document.getElementById('logs');
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    el.textContent = d.logs;
    if (atBottom) el.scrollTop = el.scrollHeight;
  }});
}}

// ── Status refresh ──────────────────────────────────────
function refreshAllStatus() {{
  document.querySelectorAll('.printer-card').forEach(card => {{
    const id = card.dataset.id;
    api('/api/status/' + id, 'GET').then(d => {{
      const colours = {{ok: 'var(--good)', error: 'var(--bad)'}};
      const labels = {{ok: 'OK', error: 'FAILED'}};
      const lbl = document.getElementById('status-label-' + id);
      const time = document.getElementById('status-time-' + id);
      const msg = document.getElementById('status-msg-' + id);
      if (lbl) {{
        lbl.textContent = labels[d.status] || 'No prints yet';
        lbl.style.color = colours[d.status] || 'var(--muted)';
      }}
      if (time) time.textContent = d.timestamp || '\u2014';
      if (msg) msg.textContent = d.message || '';
    }}).catch(() => {{}});
  }});
}}

// ── Print queue ─────────────────────────────────────────
function renderQueue(id, jobs) {{
  const box = document.getElementById('queue-' + id);
  if (!box) return;
  box.replaceChildren();
  if (!jobs || !jobs.length) {{ box.style.display = 'none'; return; }}
  const n = jobs.length;
  const el = (tag, cls, text) => {{
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }};
  const cancelBtn = (label, jobId, danger) => {{
    const b = el('button', 'btn btn-xs' + (danger ? ' btn-danger' : ''), label);
    b.addEventListener('click', () => cancelQueue(id, jobId));
    return b;
  }};
  box.className = 'queue-box' + (n > 1 ? ' queue-warn' : '');
  const head = el('div', 'queue-head');
  head.append(el('strong', '', n + ' job' + (n > 1 ? 's' : '') + ' waiting in queue'), cancelBtn('Cancel all', null, true));
  box.append(head);
  if (n > 1) {{
    const warn = el('div', '', 'If the printer comes back online, all ' + n + ' will print at once.');
    warn.style.marginTop = '4px';
    box.append(warn);
  }}
  jobs.forEach(j => {{
    const row = el('div', 'queue-job');
    row.append(el('span', '', j.id + ' \u00b7 ' + j.submitted), cancelBtn('Cancel', j.id, false));
    box.append(row);
  }});
  box.style.display = '';
}}

function refreshQueues() {{
  document.querySelectorAll('.printer-card').forEach(card => {{
    const id = card.dataset.id;
    api('/api/queue/' + id, 'GET').then(d => renderQueue(id, d.jobs)).catch(() => {{}});
  }});
}}

function cancelQueue(id, jobId) {{
  const body = jobId ? {{ job_id: jobId }} : {{}};
  api('/api/queue/' + id + '/cancel', 'POST', body).then(d => {{
    showToast(d.ok ? (jobId ? 'Job cancelled' : 'Queue cleared') : (d.message || 'Could not cancel'), d.ok ? 'success' : 'error');
    renderQueue(id, d.jobs);
  }});
}}

// ── Next print countdown ────────────────────────────────
const nextPrintTimes = {{}};
const lastMaintained = {{}};

function formatCountdown(ms) {{
  if (ms <= 0) return 'now';
  const d = Math.floor(ms / 86400000);
  const h = Math.floor((ms % 86400000) / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  let parts = [];
  if (d > 0) parts.push(d + 'd');
  if (h > 0) parts.push(h + 'h');
  parts.push(m + 'm');
  return parts.join(' ');
}}

function formatNextDate(iso) {{
  const d = new Date(iso);
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const h = d.getHours();
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
  const min = String(d.getMinutes()).padStart(2, '0');
  return `${{months[d.getMonth()]}} ${{d.getDate()}}, ${{h12}}:${{min}} ${{ampm}}`;
}}

function refreshNextPrint() {{
  document.querySelectorAll('.printer-card').forEach(card => {{
    const id = card.dataset.id;
    api('/api/next-print/' + id, 'GET').then(d => {{
      const el = document.getElementById('next-' + id);
      if (!el) return;
      if (d.paused) {{
        el.innerHTML = 'Next print: <strong style="color:var(--warn);">paused</strong>';
        delete nextPrintTimes[id];
        return;
      }}
      lastMaintained[id] = d.last_epoch ? {{ epoch: d.last_epoch, source: d.last_source }} : null;
      if (d.next_iso) {{
        nextPrintTimes[id] = new Date(d.next_iso);
        updateCountdownDisplay(id);
      }} else {{
        el.textContent = 'Next print: unknown';
      }}
    }}).catch(() => {{}});
  }});
}}

function updateCountdownDisplay(id) {{
  const el = document.getElementById('next-' + id);
  const target = nextPrintTimes[id];
  if (!el || !target) return;
  const ms = target.getTime() - Date.now();
  const dateStr = formatNextDate(target.toISOString());
  const countdown = formatCountdown(ms);
  let last = '';
  const lm = lastMaintained[id];
  if (lm) {{
    const ago = formatCountdown(Date.now() - lm.epoch * 1000);
    last = ` \u00b7 last maintained ${{esc(ago)}} ago` + (lm.source === 'activity' ? ' (printer was in use)' : '');
  }}
  el.innerHTML = `Next print: <strong>${{esc(dateStr)}}</strong> (in ${{esc(countdown)}})${{last}}`;
}}

function updateAllCountdowns() {{
  Object.keys(nextPrintTimes).forEach(updateCountdownDisplay);
}}

// ── Init ────────────────────────────────────────────────
renderChart();
initSchedulePickers();
(el => {{ el.scrollTop = el.scrollHeight; }})(document.getElementById('logs'));
probeAllPrinters();
refreshAllStatus();
refreshNextPrint();
refreshQueues();
setInterval(refreshLogs, 30000);
setInterval(refreshAllStatus, 15000);  // Re-check print status every 15s
setInterval(refreshQueues, 15000);  // Re-check queued jobs every 15s
setInterval(probeAllPrinters, 60000);  // Re-check connectivity every 60s
setInterval(refreshNextPrint, 60000);  // Re-fetch next print times every 60s
setInterval(updateAllCountdowns, 60000);  // Update countdown display every 60s
</script>
</body>
</html>"""
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Web UI running on http://0.0.0.0:{PORT}")
    server.serve_forever()
