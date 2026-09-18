# efm-stream-monitor

Checks that the Europa FM live streams are served **and that they actually carry sound**,
then publishes a status page.

## Why the sound level is checked, not just uptime

On 18 Sep 2026, between 09:57 and about 13:00, every mount on `astreaming.edi.ro` stayed
online: HTTP 200, correct bitrate, listeners connected, the encoder never disconnected.
The audio inside was digital silence — a mean volume of −91 dB, against −10 dB for normal
programming. Uptime monitoring was green for three hours while nobody could hear
anything.

The day before, the opposite failure happened: the source dropped twice (17:13 and 17:15)
and all three backends returned 404 for about a minute, with listeners falling from ~2,500
to ~700.

**Each backend is measured separately.** `astreaming.edi.ro` resolves to three Icecast
servers through round robin DNS and they fail independently: at 14:25 on 18 Sep, the
station was silent on 176.118.186.133, where 855 listeners heard nothing, while 10,442
listeners on the other two heard it normally. Checking the hostname alone returns whichever
server DNS picked, so the same mount looks fine one minute and broken the next. A mount is
reported OK only when every server is OK, and alerts name the ones that are not.

This repository catches both, plus a third failure that only shows up in a browser: the
play button switching to "playing" while the stream request never gets a connection.

## What runs

| Check | Schedule | What it does |
| --- | --- | --- |
| [`scripts/check_streams.py`](scripts/check_streams.py) | every 5 min | decodes ~10 s of each mount with ffmpeg and measures the volume |
| [`scripts/check_player.mjs`](scripts/check_player.mjs) | every 15 min | presses play on europafm.ro in headless Chrome and checks that the audio clock advances |

States per mount:

- `OK` — data flows and the mean volume is above the threshold
- `SILENT` — data flows but the volume is below `SILENCE_DB` (the 18 Sep failure)
- `DOWN` — non-200 status or undecodable stream (the 17 Sep failure)

Alerts go to Slack on state changes only, with a reminder every 30 minutes while a mount
stays broken and a recovery message naming how long the problem lasted.

## Where it runs

GitHub Actions, because ffmpeg is preinstalled on the runners, cron is built in, there is
no server to maintain and no execution time limit. The workflows commit the current state
back to the repository, which both keeps a history and refreshes the status page.

Vercel hosts the page. A serverless function is a poor fit for the checks themselves: it
would need an ~80 MB ffmpeg binary inside a strict time limit, and a monitor should be
more reliable than the thing it monitors.

## Setup

1. Push this repository to GitHub.
2. Settings → Secrets and variables → Actions → add `SLACK_WEBHOOK_URL`
   (an [incoming webhook](https://api.slack.com/messaging/webhooks)). Without it the
   checks still run and record state, they just do not notify anyone.
3. Settings → Actions → General → Workflow permissions → **Read and write permissions**,
   so the workflows can commit the state files.
4. Actions → `stream check` → *Run workflow*, to confirm it works before waiting for cron.
5. Import the repository on [Vercel](https://vercel.com/new): framework preset **Other**,
   no build command, output directory `.`. The page is `index.html` and reads
   `state/summary.json` plus the two history files. Each state commit triggers a redeploy.

Scheduled workflows on GitHub can be delayed by a few minutes under load, and are disabled
automatically after 60 days without repository activity — the state commits keep the
repository active. If the five minute interval has to be exact, run the same script from
cron on any always-on machine instead:

```cron
*/5 * * * * cd /path/to/efm-stream-monitor && SLACK_WEBHOOK_URL=https://hooks.slack.com/... python3 scripts/check_streams.py >> /var/log/efm-stream-monitor.log 2>&1
```

## Configuration

Both scripts read the environment, so nothing needs editing to change a threshold.

| Variable | Default | Notes |
| --- | --- | --- |
| `STREAM_HOST` | `https://astreaming.edi.ro:8443` | |
| `MOUNTS` | the five EDI mounts | space separated |
| `SILENCE_DB` | `-50` | programming runs −20…−8 dB, digital silence reads −91 dB |
| `SAMPLE_SECONDS` | `10` | longer samples are safer against quiet passages |
| `REPEAT_MINUTES` | `30` | `0` disables reminders while broken |
| `SAMPLE_EVERY_MINUTES` | `55` | how often a row is added to the history |
| `SLACK_WEBHOOK_URL` | empty | alerts |
| `SITE_URL` | `https://www.europafm.ro/` | player check |
| `TABS` | `0` | player check: background tabs to open first; `7` reproduces the socket limit |
| `CHROME_PATH` | `/usr/bin/google-chrome` | player check |

## Files written

- `state/summary.json` — current state per mount, read by the page
- `state/player.json` — result of the last player check
- `history/incidents.jsonl` — one line per state change
- `history/samples.jsonl` — one line per hour, for the listener graph

## Running locally

```bash
python3 scripts/check_streams.py          # needs ffmpeg
npm install && node scripts/check_player.mjs   # needs Chrome
python3 -m http.server 8080               # then open http://localhost:8080
```

Useful one-off commands when someone reports no sound:

```bash
# is there sound right now?
ffmpeg -t 10 -i https://astreaming.edi.ro:8443/EuropaFM_aac -af volumedetect -f null - 2>&1 | grep volume

# which mounts exist, since when, with how many listeners
curl -s https://astreaming.edi.ro:8443/status-json.xsl | python3 -m json.tool
```

`stream_start_iso8601` is when the encoder last connected. A value that keeps changing
means the source is dropping. A value that stays put while listeners hear nothing means
the feed into the encoder is the problem, not the streaming server.

## Background

The player fix these checks accompany lives in the site repository: the radio player used
to open a stream connection on every page load, even without pressing play. Browsers allow
six connections per host and the streaming server only speaks HTTP/1.1, so a listener with
a few tabs open got silence with no error. `preload: "none"` fixed that; these checks make
sure we hear about the next failure before listeners do.
