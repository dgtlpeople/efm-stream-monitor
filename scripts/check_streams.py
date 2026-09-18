#!/usr/bin/env python3
"""Check that the radio streams are served AND that they carry sound.

On 18 Sep 2026 every mount stayed online from 09:57 to ~13:00: HTTP 200, right
bitrate, listeners connected, source never disconnected - while the audio inside
was digital silence (-91 dB against -10 dB for normal programming). Availability
monitoring showed green for three hours, so this script decodes a sample and
measures its level too.

The host resolves to several Icecast servers behind round robin DNS and they
fail independently: on 18 Sep 2026, avstreamer3 relayed silence for EuropaFM_aac
while the other two were fine, so roughly a third of listeners heard nothing and
the rest heard the station normally. Every backend is therefore measured
separately, by address, and a mount is only OK when all of them are.

Writes state/summary.json (current state, for the status page), appends state
changes to history/incidents.jsonl and an hourly sample to history/samples.jsonl.

Exit code: 0 everything fine, 1 at least one mount broken, 2 the check itself
could not run.
"""

import json
import os
import re
import socket
import subprocess
import tempfile
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

STREAM_HOST = os.environ.get("STREAM_HOST", "https://astreaming.edi.ro:8443")
MOUNTS = os.environ.get(
    "MOUNTS", "EuropaFM_aac europafm_aacp48k VirginRadio_aac virgin_aacp_64k vibe_ro64"
).split()

# mean volume below this many dB over the sample window counts as silence
SILENCE_DB = float(os.environ.get("SILENCE_DB", "-50"))
SAMPLE_SECONDS = int(os.environ.get("SAMPLE_SECONDS", "10"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(REPO_ROOT, "state", "summary.json"))
INCIDENTS_FILE = os.environ.get(
    "INCIDENTS_FILE", os.path.join(REPO_ROOT, "history", "incidents.jsonl")
)
SAMPLES_FILE = os.environ.get("SAMPLES_FILE", os.path.join(REPO_ROOT, "history", "samples.jsonl"))
SAMPLE_EVERY_MINUTES = int(os.environ.get("SAMPLE_EVERY_MINUTES", "55"))
REPEAT_MINUTES = int(os.environ.get("REPEAT_MINUTES", "30"))

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")


def now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def backend_addresses():
    """Every address the stream host resolves to, so each server is checked."""
    host = STREAM_HOST.split("://", 1)[-1].split("/", 1)[0]
    port = 443
    if ":" in host:
        host, port = host.rsplit(":", 1)
        port = int(port)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return host, port, []
    return host, port, sorted({info[4][0] for info in infos})


def curl(url, address, host, port, args, timeout):
    """curl pinned to one backend address, keeping TLS and the Host header valid."""
    command = ["curl", "-sS", "--resolve", "%s:%d:%s" % (host, port, address)]
    command += args + [url]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def listener_counts(address, host, port, timeout=15):
    """Listeners per mount on one server, from its public Icecast status page."""
    counts = {}
    result = curl(
        STREAM_HOST + "/status-json.xsl", address, host, port, ["-m", "10"], timeout
    )
    if result is None or result.returncode != 0:
        return counts
    try:
        stats = json.loads(result.stdout)["icestats"]
    except (ValueError, KeyError):
        return counts
    sources = stats.get("source", [])
    sources = sources if isinstance(sources, list) else [sources]
    for source in sources:
        name = source.get("listenurl", "").rstrip("/").rsplit("/", 1)[-1]
        counts[name] = {
            "listeners": source.get("listeners"),
            "source_started": source.get("stream_start_iso8601"),
            "server": stats.get("host"),
        }
    return counts


def sample_volume(mount, address, host, port):
    """(http status, mean volume in dB) for one mount on one backend."""
    url = "%s/%s" % (STREAM_HOST, mount)
    with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as handle:
        path = handle.name
    try:
        result = curl(
            url, address, host, port,
            ["-o", path, "-w", "%{http_code}", "-m", str(SAMPLE_SECONDS)],
            SAMPLE_SECONDS * 3 + 15,
        )
        if result is None:
            return 0, None
        status = int(result.stdout.strip() or 0)
        # curl exits 28 when --max-time cuts a live stream, which is the normal case
        if status != 200 or os.path.getsize(path) < 4096:
            return status, None

        probe = subprocess.run(
            [FFMPEG, "-hide_banner", "-nostdin", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        found = re.findall(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", probe.stderr)
        return status, (float(found[-1]) if found else None)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0, None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def check_backend(mount, address, host, port):
    status, volume = sample_volume(mount, address, host, port)
    if status != 200:
        return {"state": "DOWN", "http": status, "volume_db": None,
                "detail": "HTTP %s - the mount is not being served" % status}
    if volume is None:
        return {"state": "DOWN", "http": status, "volume_db": None,
                "detail": "stream could not be decoded"}
    if volume < SILENCE_DB:
        return {"state": "SILENT", "http": status, "volume_db": volume,
                "detail": "mean volume %.1f dB is below %.0f dB - data flows but there is no sound"
                % (volume, SILENCE_DB)}
    return {"state": "OK", "http": status, "volume_db": volume,
            "detail": "mean volume %.1f dB" % volume}


def notify(subject, body):
    print("%s %s - %s" % (iso(now()), subject, body))
    if not SLACK_WEBHOOK_URL:
        return
    payload = json.dumps({"text": "%s\n%s" % (subject, body)}).encode("utf-8")
    request = urllib.request.Request(
        SLACK_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(request, timeout=10).read()
    except Exception as exc:
        print("slack notification failed: %s" % exc, file=sys.stderr)


def load_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def append_jsonl(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def last_sample_time():
    try:
        with open(SAMPLES_FILE, encoding="utf-8") as handle:
            last = None
            for line in handle:
                if line.strip():
                    last = line
        if not last:
            return None
        return datetime.fromisoformat(json.loads(last)["ts"].replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError):
        return None


def main():
    if subprocess.run([FFMPEG, "-version"], capture_output=True).returncode != 0:
        print("ffmpeg is required but was not found", file=sys.stderr)
        return 2

    previous = load_json(STATE_FILE, {}).get("mounts", {})
    host, port, addresses = backend_addresses()
    if not addresses:
        print("could not resolve %s" % host, file=sys.stderr)
        return 2

    listeners = {address: listener_counts(address, host, port) for address in addresses}
    moment = now()
    summary = {
        "checked_at": iso(moment),
        "silence_threshold_db": SILENCE_DB,
        "servers": addresses,
        "mounts": {},
    }
    # keep mounts this run did not check, so a partial run leaves the page complete
    for name, data in previous.items():
        if name not in MOUNTS:
            summary["mounts"][name] = data
    failed = False

    for mount in MOUNTS:
        servers = {}
        total_listeners = 0
        counted = False
        for address in addresses:
            check = check_backend(mount, address, host, port)
            info = listeners.get(address, {}).get(mount, {})
            check["listeners"] = info.get("listeners")
            check["server"] = info.get("server") or address
            check["source_started"] = info.get("source_started")
            if isinstance(check["listeners"], int):
                total_listeners += check["listeners"]
                counted = True
            servers[address] = check

        broken = {a: s for a, s in servers.items() if s["state"] != "OK"}
        # the mount is only healthy when every backend a listener may land on is
        state = "OK"
        if broken:
            states = {s["state"] for s in broken.values()}
            state = "DOWN" if "DOWN" in states else "SILENT"

        if broken:
            detail = "%d of %d servers %s: %s" % (
                len(broken), len(servers), state.lower(),
                ", ".join("%s (%s)" % (a, s["detail"]) for a, s in sorted(broken.items())),
            )
        else:
            detail = "all %d servers fine (%s)" % (
                len(servers),
                ", ".join("%.1f dB" % s["volume_db"] for _, s in sorted(servers.items())),
            )

        result = {
            "state": state,
            "detail": detail,
            "listeners": total_listeners if counted else None,
            "source_started": next(
                (s["source_started"] for s in servers.values() if s.get("source_started")), None
            ),
            "servers": servers,
        }

        was = previous.get(mount, {})
        previous_state = was.get("state")
        since = was.get("since") or iso(moment)

        if result["state"] != previous_state:
            result["since"] = iso(moment)
            append_jsonl(
                INCIDENTS_FILE,
                {
                    "ts": iso(moment),
                    "mount": mount,
                    "from": previous_state,
                    "to": result["state"],
                    "listeners": result["listeners"],
                    "detail": result["detail"],
                    "servers": {a: {"state": s["state"], "volume_db": s["volume_db"]}
                                for a, s in servers.items()},
                },
            )
            listener_note = (
                " Listeners: %s." % result["listeners"] if result["listeners"] is not None else ""
            )
            if result["state"] == "OK" and previous_state:
                started = datetime.fromisoformat(since.replace("Z", "+00:00"))
                minutes = int((moment - started).total_seconds() // 60)
                notify(
                    "[EFM] %s recovered" % mount,
                    "Back to normal after %d min in state %s. %s.%s"
                    % (minutes, previous_state, result["detail"], listener_note),
                )
            else:
                notify(
                    "[EFM] %s is %s" % (mount, result["state"]),
                    "%s.%s Listeners on a broken server hear nothing while the others are fine."
                    % (result["detail"], listener_note),
                )
        else:
            result["since"] = since
            if result["state"] != "OK" and REPEAT_MINUTES > 0:
                started = datetime.fromisoformat(since.replace("Z", "+00:00"))
                last_notified = was.get("notified_at")
                last_notified = (
                    datetime.fromisoformat(last_notified.replace("Z", "+00:00"))
                    if last_notified
                    else started
                )
                if moment - last_notified >= timedelta(minutes=REPEAT_MINUTES):
                    minutes = int((moment - started).total_seconds() // 60)
                    notify(
                        "[EFM] %s still %s" % (mount, result["state"]),
                        "Unchanged for %d min. %s." % (minutes, result["detail"]),
                    )
                    result["notified_at"] = iso(moment)
                else:
                    result["notified_at"] = was.get("notified_at")

        summary["mounts"][mount] = result
        failed = failed or result["state"] != "OK"

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
        handle.write("\n")

    # one sample per hour keeps a usable history without a commit every 5 minutes
    last = last_sample_time()
    if last is None or moment - last >= timedelta(minutes=SAMPLE_EVERY_MINUTES):
        append_jsonl(
            SAMPLES_FILE,
            {
                "ts": iso(moment),
                "mounts": {
                    name: {
                        "state": data["state"],
                        "listeners": data["listeners"],
                        "servers": {
                            address: server["volume_db"]
                            for address, server in (data.get("servers") or {}).items()
                        },
                    }
                    for name, data in summary["mounts"].items()
                },
            },
        )

    for name, data in summary["mounts"].items():
        print("%-20s %-6s %s" % (name, data["state"], data["detail"]))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
