#!/usr/bin/env python3
"""Minimal IPP client (stdlib only) for reading printer and job attributes.

Used to ask the printer itself which jobs it has completed, so prints sent from
other computers or phones count as activity, not just jobs sent through this
container's CUPS queues.

Usage (diagnostics):
    python3 ipp_client.py <ip> jobs       # recently completed jobs reported by the printer
    python3 ipp_client.py <ip> printer    # a few printer attributes
"""

import json
import os
import struct
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

# Value tags
T_INTEGER, T_BOOLEAN, T_ENUM = 0x21, 0x22, 0x23
T_DATETIME = 0x31
T_BEG_COLLECTION, T_END_COLLECTION, T_MEMBER_NAME = 0x34, 0x37, 0x4A
T_NAME, T_KEYWORD, T_URI, T_CHARSET, T_LANGUAGE = 0x42, 0x44, 0x45, 0x47, 0x48
# Delimiter tags
D_OPERATION, D_JOB, D_END, D_PRINTER = 0x01, 0x02, 0x03, 0x04

OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B


def _attr(tag, name, values):
    out = b""
    for i, v in enumerate(values):
        n = name.encode() if i == 0 else b""
        if tag == T_BOOLEAN:
            data = b"\x01" if v else b"\x00"
        elif tag in (T_INTEGER, T_ENUM):
            data = struct.pack(">i", v)
        else:
            data = v.encode()
        out += struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(data)) + data
    return out


def _request(op, uri, extra):
    body = struct.pack(">BBHI", 2, 0, op, 1) + bytes([D_OPERATION])
    body += _attr(T_CHARSET, "attributes-charset", ["utf-8"])
    body += _attr(T_LANGUAGE, "attributes-natural-language", ["en"])
    body += _attr(T_URI, "printer-uri", [uri])
    for tag, name, values in extra:
        body += _attr(tag, name, values)
    return body + bytes([D_END])


def _decode_value(tag, data):
    if tag in (T_INTEGER, T_ENUM) and len(data) == 4:
        return struct.unpack(">i", data)[0]
    if tag == T_BOOLEAN:
        return data != b"\x00"
    if tag == T_DATETIME and len(data) == 11:
        y, mo, d, h, mi, s, ds, sign, oh, om = struct.unpack(">HBBBBBBcBB", data)
        offset = timedelta(hours=oh, minutes=om) * (-1 if sign == b"-" else 1)
        return datetime(y, mo, d, h, mi, s, ds * 100000, tzinfo=timezone(offset))
    if 0x40 <= tag <= 0x5F:
        return data.decode("utf-8", "replace")
    return data


def parse_response(raw):
    """Returns (status_code, groups). Each group is (delimiter_tag, {name: [values]}).
    Collection members are flattened to dotted names, e.g. "media-col.media-type"."""
    status = struct.unpack(">H", raw[2:4])[0]
    pos, groups, current = 8, [], None
    path, member, attr_name = [], None, None
    while pos < len(raw):
        tag = raw[pos]
        pos += 1
        if tag <= 0x0F:
            if tag == D_END:
                break
            current = {}
            groups.append((tag, current))
            continue
        name_len = struct.unpack(">H", raw[pos:pos + 2])[0]
        name = raw[pos + 2:pos + 2 + name_len].decode("utf-8", "replace")
        pos += 2 + name_len
        val_len = struct.unpack(">H", raw[pos:pos + 2])[0]
        data = raw[pos + 2:pos + 2 + val_len]
        pos += 2 + val_len
        if current is None:
            continue
        if name and not path:
            attr_name = name
        if tag == T_BEG_COLLECTION:
            path.append(attr_name if not path else member)
            continue
        if tag == T_END_COLLECTION:
            if path:
                path.pop()
            continue
        if tag == T_MEMBER_NAME:
            member = data.decode("utf-8", "replace")
            continue
        key = ".".join(path + [member]) if path else attr_name
        current.setdefault(key, []).append(_decode_value(tag, data))
    return status, groups


DEFAULT_PATH = os.environ.get("PBS_IPP_PATH", "/ipp/print")  # override only for testing


def ipp_call(ip, op, extra, timeout=8, path=None):
    path = path or DEFAULT_PATH
    host = ip if ip.count(":") == 1 else f"{ip}:631"  # "ip" or "ip:port"
    uri = f"ipp://{host}{path}"
    http_url = f"http://{host}{path}"
    req = Request(http_url, data=_request(op, uri, extra),
                  headers={"Content-Type": "application/ipp"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return parse_response(resp.read())


def printer_attributes(ip, names, **kw):
    status, groups = ipp_call(ip, OP_GET_PRINTER_ATTRIBUTES,
                              [(T_KEYWORD, "requested-attributes", names)], **kw)
    attrs = {}
    for tag, g in groups:
        if tag == D_PRINTER:
            attrs.update(g)
    return attrs


JOB_ATTRS = ["job-id", "job-name", "job-state", "job-originating-user-name",
             "time-at-completed", "date-time-at-completed", "media-col", "media-type"]


def completed_jobs(ip, limit=50, **kw):
    """Completed jobs as reported by the printer, newest first:
    [{"id", "name", "user", "completed" (aware datetime or None), "media_type"}]"""
    status, groups = ipp_call(ip, OP_GET_JOBS, [
        (T_NAME, "requesting-user-name", ["nozzlewake"]),
        (T_KEYWORD, "which-jobs", ["completed"]),
        (T_BOOLEAN, "my-jobs", [False]),
        (T_INTEGER, "limit", [limit]),
        (T_KEYWORD, "requested-attributes", JOB_ATTRS),
    ], **kw)
    if status >= 0x0400:
        raise RuntimeError(f"Get-Jobs refused (status 0x{status:04x})")
    jobs = [g for tag, g in groups if tag == D_JOB]

    # time-at-completed is printer uptime in seconds; date-time-at-completed is the
    # printer's clock, which may be off. Both are mapped onto this machine's clock.
    up, skew = None, timedelta(0)
    try:
        pa = printer_attributes(ip, ["printer-up-time", "printer-current-time"], **kw)
        local_now = datetime.now(timezone.utc)
        up = (pa.get("printer-up-time") or [None])[0]
        printer_now = (pa.get("printer-current-time") or [None])[0]
        if isinstance(printer_now, datetime):
            skew = local_now - printer_now
        up = (up, local_now) if isinstance(up, int) else None
    except Exception:
        pass

    out = []
    for j in jobs:
        first = lambda k: (j.get(k) or [None])[0]
        when = first("date-time-at-completed")
        if isinstance(when, datetime):
            when = when + skew
        else:
            when = None
            t = first("time-at-completed")
            if isinstance(t, int) and t > 0 and up:
                when = up[1] - timedelta(seconds=up[0] - t)
        out.append({
            "id": first("job-id"),
            "name": first("job-name") or "",
            "user": first("job-originating-user-name") or "",
            "state": first("job-state"),
            "completed": when,
            "media_type": first("media-col.media-type") or first("media-type") or "",
        })
    out.sort(key=lambda x: x["completed"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[2] not in ("jobs", "printer"):
        print(__doc__)
        sys.exit(2)
    ip = sys.argv[1]
    if sys.argv[2] == "jobs":
        jobs = completed_jobs(ip)
        print(f"{len(jobs)} completed job(s) reported by {ip}:")
        for j in jobs:
            when = j["completed"].astimezone().strftime("%Y-%m-%d %H:%M:%S") if j["completed"] else "time unknown"
            print(f"  #{j['id']}  {when}  media-type={j['media_type'] or '?'}  "
                  f"user={j['user'] or '?'}  name={j['name'] or '?'}")
    else:
        attrs = printer_attributes(ip, ["printer-make-and-model", "printer-up-time", "printer-state",
                                        "marker-names", "marker-levels", "media-type-supported"])
        print(json.dumps({k: [str(x) for x in v] for k, v in attrs.items()}, indent=2))
