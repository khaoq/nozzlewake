###############################################################################
# NozzleWake (formerly print-blockage-stopper)
#
# All-in-one container that keeps any IPP-capable network printer's print head
# healthy by sending a small test print on a schedule. Supports multiple
# printers, mDNS discovery, custom test images, and a web dashboard.
#
# v2.2: Native Discord/Slack/ntfy webhooks, paper type per entry, interval schedules,
#       real-use detection via the printer's job history, low-ink alerts
###############################################################################

FROM debian:bookworm-slim

LABEL org.opencontainers.image.title="NozzleWake"
LABEL org.opencontainers.image.description="Automated maintenance prints for any network printer (IPP Everywhere compatible). Based on ahmedbadr3/print-blockage-stopper."
LABEL org.opencontainers.image.source="https://github.com/khaoq/nozzlewake"
LABEL org.opencontainers.image.licenses="MIT"

# ── Install packages ─────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        cups \
        cups-client \
        cups-filters \
        cups-ipp-utils \
        printer-driver-gutenprint \
        avahi-utils \
        cron \
        curl \
        ca-certificates \
        python3 \
        python3-pip \
        fonts-dejavu-core \
    && pip3 install --break-system-packages --no-cache-dir Pillow \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── CUPS configuration ───────────────────────────────────────
# Read-only status is visible on the LAN (port 631); printing, pausing,
# cancelling and admin are only allowed from inside the container.
# The entrypoint re-applies this file on every start, so persisted /data/cups
# configs from older versions are updated too.
COPY <<'CUPSD_CONF' /app/cupsd.conf
LogLevel warn
MaxLogSize 1m
Listen 0.0.0.0:631
ServerAlias *
DefaultEncryption Never
WebInterface Yes
Browsing No
DefaultShared No

<Location />
  Order allow,deny
  Allow all
</Location>

<Location /admin>
  Order allow,deny
  Allow localhost
</Location>

<Location /admin/conf>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
  Allow localhost
</Location>

<Policy default>
  JobPrivateAccess default
  JobPrivateValues default
  SubscriptionPrivateAccess default
  SubscriptionPrivateValues default

  # Status queries: anyone who can reach port 631
  <Limit Get-Printer-Attributes Get-Jobs Get-Job-Attributes CUPS-Get-Printers CUPS-Get-Default CUPS-Get-Classes>
    Order allow,deny
    Allow all
  </Limit>

  # Printer administration: root inside the container only
  <Limit CUPS-Add-Modify-Printer CUPS-Delete-Printer CUPS-Add-Modify-Class CUPS-Delete-Class CUPS-Set-Default>
    AuthType Default
    Require user @SYSTEM
    Order allow,deny
    Allow localhost
  </Limit>

  # Everything else (print, pause, cancel, accept/reject...): inside the container only
  <Limit All>
    Order allow,deny
    Allow localhost
  </Limit>
</Policy>
CUPSD_CONF
RUN cp /app/cupsd.conf /etc/cups/cupsd.conf

# ── Create app directories ───────────────────────────────────
RUN mkdir -p /app /app/presets /data/logs /data/uploads

# ── Copy application files ───────────────────────────────────
COPY test-image/pro1100-test-print.png /app/test-print.png
COPY scripts/generate-presets.py /app/generate-presets.py
COPY scripts/entrypoint.sh /app/entrypoint.sh
COPY scripts/auto-print.sh /app/auto-print.sh
COPY scripts/webui.py /app/webui.py
COPY scripts/printer_probe.py /app/printer_probe.py
COPY scripts/stamp_image.py /app/stamp_image.py
COPY scripts/notify.py /app/notify.py
COPY scripts/pbs_state.py /app/pbs_state.py
COPY scripts/ipp_client.py /app/ipp_client.py
COPY scripts/ink-check.py /app/ink-check.py
COPY favicon.ico /app/favicon.ico

RUN chmod +x /app/entrypoint.sh /app/auto-print.sh

# ── Generate preset test images at build time ─────────────────
RUN python3 /app/generate-presets.py /app/presets

# ── Volumes ──────────────────────────────────────────────────
# /data persists logs, printer config, uploads, and CUPS state
VOLUME ["/data"]

# ── Environment variables (user-configurable) ────────────────
# PRINTER_IP:    (Optional) Auto-add this printer on first boot
# PRINTER_PORT:  Port for socket connection (default 9100)
# SCHEDULE:      Default cron expression for new printers
# PAPER_SIZE:    Default media size (A4, Letter, etc.)
# CONNECTION:    Default connection type: ipp or socket
# SKIP_HOURS:    Default skip-if-recently-printed window (hours)
# WEBHOOK_URL:   (Optional) URL for success/failure notifications
# DASHBOARD_PASSWORD: (Optional) Require this password for the dashboard/API
# ALLOWED_HOSTS: Extra hostnames the dashboard answers to (e.g. behind a reverse proxy)
# JOB_TIMEOUT:   Seconds to wait for a print to complete before cancelling it
# FAILURE_ALERT_THRESHOLD: Alert once this many runs in a row have failed
ENV PRINTER_IP="" \
    PRINTER_PORT="9100" \
    SCHEDULE="0 10 */3 * *" \
    PAPER_SIZE="A4" \
    CONNECTION="ipp" \
    SKIP_HOURS="72" \
    WEBHOOK_URL="" \
    DASHBOARD_PASSWORD="" \
    ALLOWED_HOSTS="" \
    JOB_TIMEOUT="600" \
    FAILURE_ALERT_THRESHOLD="2"

# ── Expose ports ──────────────────────────────────────────────
EXPOSE 631
EXPOSE 8631

# ── Health check ─────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -sf http://localhost:8631/healthz || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
