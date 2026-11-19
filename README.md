# NozzleWake

*Based on [print-blockage-stopper](https://github.com/ahmedbadr3/print-blockage-stopper) by ahmedbadr3 (MIT).*

Automated maintenance prints for **any network printer** (IPP Everywhere compatible). Keeps pigment inkjet print heads healthy by sending small test prints on a schedule.

## Why?

Pigment-based inkjet printers — especially large-format models like Canon imagePROGRAF, Epson SureColor, and HP DesignJet — clog when they sit idle. The pigment settles in the nozzles, leading to expensive cleaning cycles, wasted ink, and sometimes permanent damage. This container prevents that by automatically printing a tiny test image every few days.

## Quick Start

```bash
docker run -d \
  --name nozzlewake \
  -e PRINTER_IP=192.168.1.50 \
  -p 8631:8631 \
  -p 631:631 \
  -v /path/to/data:/data \
  ghcr.io/khaoq/nozzlewake
```

Open **http://YOUR_HOST:8631** to access the dashboard. You can also skip `PRINTER_IP` and add printers from the dashboard.

### Unraid

Add the template from this repo (`unraid-ca-template.xml`): copy it to `/boot/config/plugins/dockerMan/templates-user/my-NozzleWake.xml` on the Unraid flash drive, then **Docker → Add Container → Template: NozzleWake**.

## Features

- **Multi-printer support** — manage multiple printers from one container
- **Network auto-discovery** — find printers via mDNS/IPP
- **Interval scheduling** — "every N days" counts from the last maintenance print, so prints never bunch up at month ends, and a failed or missed run is retried the next day
- **Smart skip** — skips the maintenance print if the printer was recently used on the same kind of paper (plain vs photo), including prints from other computers and phones when the printer reports its job history over IPP
- **Low-ink alerts** — one notification per ink when it drops below a set level (default 15%), re-armed after a refill
- **Preset test images** — optimised for 4, 6, 8, 11, and 12-colour printers
- **Custom image upload** — use your own test pattern (up to 5 MB)
- **Web dashboard** — status, schedule controls, print history chart
- **Connection status** — real-time green/red indicators
- **Ink levels** — live ink level bars via IPP attributes
- **Auto-retry** — retries failed prints once after 30 seconds
- **No print backlog** — unconfirmed jobs are cancelled and offline printers are skipped, so nothing piles up to print all at once later
- **Queue management** — see jobs waiting for each printer and cancel one or all from the dashboard
- **Failure alerts** — alerts once several runs in a row fail (default 2), and again when printing recovers
- **Optional password** — set `DASHBOARD_PASSWORD` to protect the dashboard and API
- **Notifications** — webhook (Slack/Discord/ntfy), email (SMTP), and Home Assistant
- **CSV export** — download print history
- **Mobile-friendly** — responsive layout
- **Unraid notifications** — alerts on print failures

## Configuration

All settings are optional. Printers can be added entirely from the dashboard.

| Variable | Default | Description |
|---|---|---|
| `PRINTER_IP` | *(empty)* | Auto-add this printer on first boot |
| `SCHEDULE` | `0 10 */3 * *` | Default cron schedule |
| `PAPER_SIZE` | `A4` | Default paper size (A4, Letter, A3, etc.) |
| `SKIP_HOURS` | `72` | Skip if printer was used within N hours |
| `CONNECTION` | `ipp` | Connection type: `ipp` or `socket` |
| `PRINTER_PORT` | `9100` | Socket port (only with `CONNECTION=socket`) |
| `WEBHOOK_URL` | *(empty)* | Webhook URL for notifications |
| `DASHBOARD_PASSWORD` | *(empty)* | If set, the dashboard and API require this password (HTTP Basic auth, any username). Recommended |
| `ALLOWED_HOSTS` | *(empty)* | Extra hostnames the dashboard answers to, comma separated (e.g. `printers.example.com`, or `.example.com` for subdomains). Needed only when using a domain name, such as behind a reverse proxy; IPs, single-word and `.local`/`.lan`/`.home.arpa` names always work. `*` turns the check off |
| `JOB_TIMEOUT` | `600` | Seconds to wait for a print to complete before cancelling it |
| `FAILURE_ALERT_THRESHOLD` | `2` | Alert once this many runs in a row have failed |

### Schedule Examples

| Expression | Meaning |
|---|---|
| `0 10 */3 * *` | Every 3 days at 10 AM |
| `0 10 */5 * *` | Every 5 days at 10 AM |
| `0 8 * * 1,4` | Monday and Thursday at 8 AM |
| `0 9 * * *` | Daily at 9 AM |

## Notifications

Configure notifications from the dashboard under the **Notifications** card. Three independent channels:

**Webhook** — One or more URLs (one per line). Discord (`discord.com/api/webhooks/...`), Slack (`hooks.slack.com`) and `ntfy.sh` URLs are sent in each service's own format; any other URL gets a JSON POST with `event`, `printer`, `printer_id`, `message`, `timestamp`, plus a readable `text`/`content` field. The **Test** button now shows the service's actual error if delivery fails, and failures are written to the dashboard log.

**Same printer, several schedules** — A printer can be added more than once (or use the copy icon on its card). Each entry has its own schedule, test image and **Paper type**. On Epson printers with two blacks (e.g. ET-8500/8550), the paper type picks the black ink: plain or matte paper uses Matte Black, glossy or semi-gloss photo paper uses Photo Black. One entry on *Plain paper* and one on *Photo paper (glossy)* exercises every ink.

**Email** — SMTP configuration (server, port, from/to, optional auth). Works with Gmail, Outlook, Mailgun, or any SMTP provider.

**Home Assistant** — Creates persistent notifications via the HA REST API. Requires a long-lived access token (HA profile > Long-Lived Access Tokens > Create Token). Supports self-signed certificates.

Each channel has a **Test** button to verify your setup.

## Ports

| Port | Purpose |
|---|---|
| `8631` | Dashboard |
| `631` | CUPS web UI (advanced) |

## Volumes

| Path | Purpose |
|---|---|
| `/data` | Printer config, logs, uploads, CUPS state |

## How It Works

1. On startup, the container runs CUPS and registers your printers
2. Cron runs `auto-print.sh` for each printer daily at its chosen time; it only prints once the printer's interval (e.g. 5 days) has passed since its last maintenance
3. It checks whether the printer was recently used with the same black ink (smart skip): the printer's own completed-job list, plus other entries for the same printer. A skip counts as maintenance, so the interval restarts from that real use
4. It cancels any stale queued jobs
5. If the printer is unreachable it records a failure and queues nothing
6. Otherwise it sends a small test image via CUPS/IPP and waits up to `JOB_TIMEOUT` for it to complete; a job that isn't confirmed is cancelled so it can't print later
7. Results are logged, and alerts go to enabled channels once `FAILURE_ALERT_THRESHOLD` runs in a row have failed (then every 7th further failure, since failed runs are retried daily)
8. An hourly ink check sends a low-ink alert once per ink
9. The web dashboard (Python HTTP server on port 8631) provides controls and monitoring

## Requirements

- Any network printer with IPP Everywhere support
- Printer powered on with paper loaded
- Network access from Docker host to printer

## License

MIT

### Checking what your printer reports

To see whether your printer shares its job history (used by smart skip to notice prints from other devices), run inside the container:

```
python3 /app/ipp_client.py <printer-ip> jobs
```

If it lists recent jobs with times, smart skip will see prints from other devices. If the printer reports no paper type for a job, the job is treated as plain paper.

## Security notes

- Set `DASHBOARD_PASSWORD`; without it anyone who can reach port 8631 can change settings. The password is sent over plain HTTP, so use a reverse proxy with HTTPS if you reach it from outside your network, and don't expose port 8631 to the internet.
- The dashboard only answers to IP addresses, LAN-style hostnames and names in `ALLOWED_HOSTS`, which blocks DNS-rebinding attacks from websites. All changes also require a custom request header, so other sites can't submit forms to it.
- Port 631 (CUPS) is read-only from the network: status pages and printer queries work, but printing, pausing, cancelling and admin are only possible from inside the container. If you don't use the CUPS status page you can leave port 631 unmapped.
- Webhook and Home Assistant URLs may point at LAN addresses but not at the container itself or link-local/cloud-metadata addresses (also checked after DNS lookup), and redirects aren't followed.

