#!/usr/bin/env python3
"""Unified notification dispatcher for NozzleWake.

Usage:
    python3 notify.py --event print_ok --printer "Name" --printer-id "id" --message "text"

Reads notification config from /data/printers.json and dispatches to all
enabled channels: webhook, email (SMTP), Home Assistant.
"""

import argparse
import json
import ipaddress
import os
import re
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib.error import HTTPError, URLError

DATA_FILE = "/data/printers.json"


def load_config(config_path=None):
    """Load global notification config from printers.json or a custom path."""
    try:
        path = config_path or DATA_FILE
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("global", {})
    except Exception:
        return {}


def _bad_address(addr):
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_unspecified:
        return "URL cannot point to this machine (localhost)"
    if addr.is_link_local:  # includes the 169.254.169.254 cloud metadata endpoint
        return "URL cannot point to a link-local/metadata address"
    if addr.is_multicast or addr.is_reserved:
        return "URL cannot point to a multicast/reserved address"
    return None


def validate_url(url, resolve=True):
    """Return an error string if the URL is unsafe to POST to, else None.

    LAN addresses are allowed (Home Assistant, self-hosted ntfy...), but not the
    container itself or link-local/metadata addresses. Hostnames are resolved, so
    a name that points at 127.0.0.1 is caught too. Unresolvable names pass here;
    the request itself will fail.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "URL must be http or https"
    hostname = (parsed.hostname or "").strip(".").lower()
    if not hostname:
        return "URL has no host"
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return "URL cannot point to this machine (localhost)"
    try:
        addrs = [ipaddress.ip_address(hostname)]
    except ValueError:
        addrs = []
        if resolve:
            try:
                infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
                addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
            except (socket.gaierror, UnicodeError, ValueError):
                addrs = []
    for addr in addrs:
        err = _bad_address(addr)
        if err:
            return err
    return None


class _NoRedirect(HTTPRedirectHandler):
    """Never follow redirects: a redirect could point at an address validate_url blocks."""
    def redirect_request(self, *args, **kwargs):
        return None


def _open(req, timeout, context=None):
    handlers = [_NoRedirect()]
    if context is not None:
        handlers.append(HTTPSHandler(context=context))
    return build_opener(*handlers).open(req, timeout=timeout)


def parse_webhook_urls(value):
    """The webhook setting may hold several URLs (one per line, or comma separated)."""
    return [u for u in re.split(r"[\s,]+", value or "") if u]


# Discord and Cloudflare reject urllib's default "Python-urllib/x.y" agent (HTTP 403,
# error 1010), so always identify ourselves.
USER_AGENT = "NozzleWake/2.2"

EVENT_LABELS = {
    "print_completed": ("\u2705", "Print completed"),
    "print_recovered": ("\u2705", "Printing again"),
    "print_ok": ("\U0001F5A8\uFE0F", "Print submitted"),
    "print_stopped": ("\u26A0\uFE0F", "Print stopped"),
    "print_timeout": ("\u23F3", "Print unconfirmed"),
    "print_failed": ("\u274C", "Print failed"),
    "print_offline": ("\u26A0\uFE0F", "Printer offline"),
    "ink_low": ("\U0001F4A7", "Ink low"),
    "test": ("\U0001F514", "Test notification"),
}


# Sent only when notify_on is "all" (the default)
ROUTINE_EVENTS = {"print_ok", "print_completed"}


def chat_text(payload, bold="**"):
    """Human-readable message for chat services (Discord markdown by default)."""
    icon, label = EVENT_LABELS.get(payload["event"], ("", payload["event"]))
    head = f"{icon} {bold}{label}{bold}".strip()
    if payload.get("printer") and payload["event"] != "test":
        head += f" \u2014 {payload['printer']}"
    msg = payload.get("message") or ""
    if payload["event"] == "test":
        msg = "NozzleWake notifications are working."
    return f"{head}\n{msg}" if msg else head


def webhook_request(url, payload):
    """Build (body, headers) in the format the destination service expects."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    headers = {"User-Agent": USER_AGENT}

    if host.endswith(("discord.com", "discordapp.com")) and "/api/webhooks/" in parsed.path:
        body = {"content": chat_text(payload)[:2000]}
    elif host == "hooks.slack.com":
        body = {"text": chat_text(payload, bold="*")}
    elif host == "ntfy.sh" or host.endswith(".ntfy.sh"):
        # ntfy displays the raw request body, so send plain text with a title header
        icon, label = EVENT_LABELS.get(payload["event"], ("", payload["event"]))
        headers["Title"] = f"{label} - {payload.get('printer') or 'NozzleWake'}"
        headers["Tags"] = "printer"
        headers["Content-Type"] = "text/plain; charset=utf-8"
        msg = chat_text(payload, bold="").split("\n", 1)
        return (msg[1] if len(msg) > 1 else msg[0]).encode("utf-8"), headers
    else:
        # Generic JSON (Home Assistant webhooks, n8n, custom receivers...). "text" and
        # "content" make it readable by Slack/Discord-compatible services too.
        text = chat_text(payload)
        body = dict(payload, text=text, content=text[:2000])

    headers["Content-Type"] = "application/json"
    return json.dumps(body).encode("utf-8"), headers


def redact(url):
    """Webhook URLs embed their secret token; never log the full thing."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path[:24]}..."


def post_webhook(url, payload):
    """POST to one webhook. Returns None on success, an error string on failure."""
    err = validate_url(url)
    if err:
        return f"{redact(url)}: {err}"
    data, headers = webhook_request(url, payload)
    for attempt in range(2):
        try:
            _open(Request(url, data=data, headers=headers, method="POST"), timeout=10).close()
            return None
        except HTTPError as e:
            detail = e.read(300).decode("utf-8", "replace").strip()
            if e.code == 429 and attempt == 0:  # rate limited: wait as asked, then retry once
                try:
                    wait = float(e.headers.get("Retry-After", "2"))
                except ValueError:
                    wait = 2
                time.sleep(min(max(wait, 0.5), 10))
                continue
            return f"{redact(url)}: HTTP {e.code} {e.reason}" + (f" \u2014 {detail}" if detail else "")
        except (URLError, OSError) as e:
            return f"{redact(url)}: {getattr(e, 'reason', e)}"
    return f"{redact(url)}: rate limited"


def send_webhook(config, payload):
    """Send the notification to every configured webhook URL. Returns True if all succeeded."""
    urls = parse_webhook_urls(config.get("webhook_url", "")) or parse_webhook_urls(os.environ.get("WEBHOOK_URL", ""))
    if not urls:
        return None
    ok = True
    for url in urls:
        err = post_webhook(url, payload)
        if err:
            log(f"Webhook failed: {err}")
            ok = False
        else:
            log(f"Webhook sent: {redact(url)}")
    return ok


def send_email(config, subject, body_text, force=False):
    """Send email notification via SMTP."""
    email_cfg = config.get("email", {})
    if not force and not email_cfg.get("enabled"):
        return None
    server = email_cfg.get("smtp_server", "").strip()
    port = int(email_cfg.get("smtp_port", 587))
    from_addr = email_cfg.get("smtp_from", "").strip()
    to_addr = email_cfg.get("smtp_to", "").strip()
    username = email_cfg.get("smtp_username", "").strip()
    password = email_cfg.get("smtp_password", "")
    use_tls = email_cfg.get("smtp_tls", True)

    if not server or not from_addr or not to_addr:
        log("Email skipped: missing server, from, or to address")
        return False

    try:
        msg = MIMEText(body_text)
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr

        if use_tls:
            smtp = smtplib.SMTP(server, port, timeout=10)
            smtp.starttls()
        else:
            smtp = smtplib.SMTP(server, port, timeout=10)

        if username and password:
            smtp.login(username, password)
        smtp.sendmail(from_addr, [to_addr], msg.as_string())
        smtp.quit()
        return True
    except Exception as e:
        log(f"Email failed: {e}")
        return False


def send_homeassistant(config, title, message, force=False):
    """Send Home Assistant persistent notification."""
    ha_cfg = config.get("homeassistant", {})
    if not force and not ha_cfg.get("enabled"):
        return None
    ha_url = ha_cfg.get("ha_url", "").strip().rstrip("/")
    ha_token = ha_cfg.get("ha_token", "").strip()
    verify_ssl = ha_cfg.get("ha_verify_ssl", True)

    if not ha_url or not ha_token:
        log("Home Assistant skipped: missing URL or token")
        return False

    err = validate_url(ha_url)
    if err:
        log(f"Home Assistant skipped: {err}")
        return False

    endpoint = f"{ha_url}/api/services/persistent_notification/create"
    payload = json.dumps({"title": title, "message": message}).encode("utf-8")
    req = Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ha_token}",
        },
        method="POST",
    )

    try:
        ctx = None
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        _open(req, timeout=10, context=ctx).close()
        return True
    except Exception as e:
        log(f"Home Assistant failed: {e}")
        return False


def log(msg):
    """Log to stderr (auto-print.sh appends it to the dashboard log)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [notify] {msg}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Send notifications for print events")
    parser.add_argument("--event", required=True,
                        choices=["print_ok", "print_completed", "print_recovered", "print_failed", "print_stopped",
                                 "print_timeout", "print_offline", "ink_low", "test"])
    parser.add_argument("--printer", default="")
    parser.add_argument("--printer-id", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--channel", default="all", choices=["all", "webhook", "email", "homeassistant"],
                        help="Send to a specific channel only (for testing)")
    parser.add_argument("--config", default=None,
                        help="Path to a JSON config file (overrides /data/printers.json)")
    args = parser.parse_args()

    config = load_config(args.config)
    now = datetime.now(timezone.utc).isoformat()

    # Build payloads
    webhook_payload = {
        "event": args.event,
        "printer": args.printer,
        "printer_id": args.printer_id,
        "message": args.message,
        "timestamp": now,
    }

    ha_title = "NozzleWake"

    if args.event == "print_completed":
        subject = f"Print Completed — {args.printer}"
        body = f"Print job completed successfully for {args.printer}.\n\n{args.message}\n\nTimestamp: {now}"
        ha_message = f"✅ Print completed: {args.printer}"
    elif args.event == "print_recovered":
        subject = f"Print Recovered — {args.printer}"
        body = f"Maintenance prints are working again for {args.printer}.\n\n{args.message}\n\nTimestamp: {now}"
        ha_message = f"✅ Printing again: {args.printer}"
    elif args.event == "print_offline":
        subject = f"Printer OFFLINE — {args.printer}"
        body = f"{args.printer} could not be reached, so no maintenance print was sent (nothing was queued).\n\n{args.message}\n\nTimestamp: {now}"
        ha_message = f"⚠️ Printer offline: {args.printer} — maintenance print skipped"
    elif args.event == "ink_low":
        subject = f"Ink Low — {args.printer}"
        body = f"{args.printer} is running low on ink.\n\n{args.message}\n\nTimestamp: {now}"
        ha_message = f"\U0001F4A7 Ink low: {args.printer} — {args.message}"
    elif args.event == "print_ok":
        # Legacy / submission-only confirmation
        subject = f"Print OK — {args.printer}"
        body = f"Print job submitted for {args.printer}.\n\n{args.message}\n\nTimestamp: {now}"
        ha_message = f"Print submitted: {args.printer} — {args.message}"
    elif args.event == "print_stopped":
        subject = f"Print STOPPED — {args.printer}"
        body = f"Print job was stopped by the printer for {args.printer}.\n\nThis usually means a hardware issue (paper tray not down, out of paper, ink issue, or paper jam).\n\n{args.message}\n\nTimestamp: {now}"
        ha_message = f"⚠️ Print stopped: {args.printer} — check printer"
    elif args.event == "print_timeout":
        subject = f"Print Unconfirmed — {args.printer}"
        body = f"Print job was submitted for {args.printer} but completion could not be confirmed within the monitoring window, so it was cancelled.\n\nCheck the printer (power, paper, ink).\n\n{args.message}\n\nTimestamp: {now}"
        ha_message = f"⏳ Print unconfirmed: {args.printer} — check printer"
    elif args.event == "print_failed":
        subject = f"Print FAILED — {args.printer}"
        body = f"Print job failed for {args.printer}.\n\n{args.message}\n\nTimestamp: {now}"
        ha_message = f"❌ Print failed: {args.printer} — {args.message}"
    else:  # test
        subject = "NozzleWake — Test Notification"
        body = "This is a test notification from NozzleWake."
        ha_message = "This is a test notification."
        webhook_payload["event"] = "test"
        webhook_payload["message"] = "Test notification"

    # "Problems only" mode: routine successes are not sent anywhere
    if config.get("notify_on") == "problems" and args.event in ROUTINE_EVENTS:
        sys.exit(0)

    # Dispatch to channels (force=True for test events to bypass enabled check)
    channel = args.channel
    force = args.event == "test"
    results = []
    if channel in ("all", "webhook"):
        r = send_webhook(config, webhook_payload)
        if r is None and channel == "webhook":
            log("Webhook skipped: no URL configured")
            r = False
        results.append(r)
    if channel in ("all", "email"):
        results.append(send_email(config, subject, body, force=force))
    if channel in ("all", "homeassistant"):
        results.append(send_homeassistant(config, ha_title, ha_message, force=force))

    # Non-zero exit when any attempted channel failed, so the dashboard's Test
    # button reports the real error instead of a false "sent!".
    sys.exit(1 if False in results else 0)


if __name__ == "__main__":
    main()
