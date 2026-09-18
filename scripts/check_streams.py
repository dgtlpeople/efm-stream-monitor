#!/usr/bin/env python3
"""Check that the radio streams are served AND that they carry sound.

On 18 Sep 2026 every mount stayed online from 09:57 to ~13:00: HTTP 200, right
bitrate, listeners connected, source never disconnected - while the audio inside
was digital silence (-91 dB against -10 dB for normal programming). Availability
monitoring showed green for three hours, so this script decodes a sample and
measures its level too.

Writes state/summary.json (current state, for the status page), appends state
changes to history/incidents.jsonl and an hourly sample to history/samples.jsonl.

Exit code: 0 everything fine, 1 at least one mount broken, 2 the check itself
could not run.
"""

import json
import os
import re
import subprocess
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


def http_status(url, timeout=10):
    request = urllib.request.Request(url, headers={"User-Agent": "efm-stream-monitor"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(64 * 1024)  # touch the body so a dead mount cannot fake a 200
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def listener_counts(timeout=10):
    """Listeners per mount, from the public Icecast status page."""
    counts = {}
    try:
        request = urllib.request.Request(
            STREAM_HOST + "/status-json.xsl", headers={"User-Agent": "efm-stream-monitor"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            stats = json.loads(response.read().decode("utf-8", "replace"))["icestats"]
    except Exception:
        return counts
    sources = stats.get("source", [])
    sources = sources if isinstance(sources, list) else [sources]
    for source in sources:
        name = source.get("listenurl", "").rstrip("/").rsplit("/", 1)[-1]
        counts[name] = {
            "listeners": source.get("listeners"),
            "source_started": source.get("stream_start_iso8601"),
        }
    return counts


def mean_volume(mount):
    """Mean volume in dB over the sample window, or None when undecodable."""
    command = [
        FFMPEG, "-hide_banner", "-nostdin",
        "-rw_timeout", str(SAMPLE_SECONDS * 2_000_000),
        "-t", str(SAMPLE_SECONDS),
        "-i", "%s/%s" % (STREAM_HOST, mount),
        "-af", "volumedetect", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=SAMPLE_SECONDS * 4 + 30
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    found = re.findall(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
    return float(found[-1]) if found else None


def check_mount(mount, listeners):
    status = http_status("%s/%s" % (STREAM_HOST, mount))
    if status != 200:
        return {
            "state": "DOWN",
            "http": status,
            "volume_db": None,
            "detail": "HTTP %s - the mount is not being served" % status,
        }

    volume = mean_volume(mount)
    if volume is None:
        return {
            "state": "DOWN",
            "http": status,
            "volume_db": None,
            "detail": "stream could not be decoded",
        }
    if volume < SILENCE_DB:
        return {
            "state": "SILENT",
            "http": status,
            "volume_db": volume,
            "detail": "mean volume %.1f dB is below %.0f dB - data flows but there is no sound"
            % (volume, SILENCE_DB),
        }
    return {
        "state": "OK",
        "http": status,
        "volume_db": volume,
        "detail": "mean volume %.1f dB" % volume,
    }


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
    listeners = listener_counts()
    moment = now()
    summary = {"checked_at": iso(moment), "silence_threshold_db": SILENCE_DB, "mounts": {}}
    # keep mounts this run did not check, so a partial run leaves the page complete
    for name, data in previous.items():
        if name not in MOUNTS:
            summary["mounts"][name] = data
    failed = False

    for mount in MOUNTS:
        result = check_mount(mount, listeners)
        info = listeners.get(mount, {})
        result["listeners"] = info.get("listeners")
        result["source_started"] = info.get("source_started")

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
                    "volume_db": result["volume_db"],
                    "listeners": result["listeners"],
                    "detail": result["detail"],
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
            elif result["state"] != "OK":
                notify(
                    "[EFM] %s is %s" % (mount, result["state"]),
                    "%s.%s Stream: %s/%s"
                    % (result["detail"], listener_note, STREAM_HOST, mount),
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
                        "volume_db": data["volume_db"],
                        "listeners": data["listeners"],
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
