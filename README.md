# cargate

A car alarm built from a spare Linux phone.

cargate turns a phone that lives in your car into a tamper alarm using the
sensors it already has: a daemon watches the magnetometer, light, and
orientation sensors over D-Bus (Ubuntu Touch's `com.nokia.SensorService`)
and sends a Telegram alert when something changes that shouldn't. On a
trigger it can SSH into a second phone on the car's WiFi and pull photo and
audio evidence before anyone can take the first phone.

No app, no cloud, no subscription — a phone, a SIM, a bot token.

## Why a phone

The phone is the best alarm hardware you already own: it has a
magnetometer, an ambient light sensor, LTE, a camera, and a battery backup
for when the car's 12V is cut. The magnetometer is the star — a car door
opening, a seat tilting, a steel tool coming closer all bend the local
magnetic field measurably, and unlike a camera it works in the dark, under
a seat, and draws milliwatts.

## What it does

- **Tamper detection** — magnetometer (percentage change + absolute
  floor), light (sudden illumination), orientation (device moved).
  Thresholds are tunable constants at the top of `cargate.py`.
- **Sustained-offset re-baselining** — a car that drives to a new parking
  spot sees a one-time *step* in magnetic field that persists; a person
  rummaging produces *oscillation*. cargate tracks offset direction: a
  sustained one-direction offset for 120 s re-baselines silently, an
  oscillating one alerts.
- **Location context** — on trigger, the alert can embed a WiFi
  fingerprint, cell tower ID, and coarse IP geolocation from
  `location_tracker.py` (no GPS needed).
- **Evidence capture** — on trigger, `capture_hook.py` SSHes into the
  peripheral phone, runs `capture.sh` (photo + 10 s audio), pulls the
  files, downscales with ffmpeg, and delivers them to Telegram.
- **Offline-tolerant delivery** — captures are *always* taken and pulled;
  if the metered LTE uplink is too slow for photos, they queue in
  `~/captures/pending/` and a timer flushes them when the phone next sees
  your home WiFi. Text alerts go through even on a throttled uplink.

## Why the WiFi honeypot is NOT in here

An earlier design tried WiFi presence detection: broadcast an open AP
honeypot, whitelist MACs, alert on strangers. It is fundamentally broken
on 2026 hardware, and the failure modes are worth recording:

- **Modern phones don't auto-join open APs.** Android 10+ and iOS 14+
  require explicit confirmation before associating. Nobody ever joins your
  honeypot.
- **MAC randomization kills whitelists.** Probing phones rotate MACs, so
  the whitelist sees a stranger every time — including your own.
- **No monitor mode on the Qualcomm client chip.** Passive probe-request
  sniffing is impossible; AP mode only sees full associations.
- **AP landscape monitoring is noise when mobile.** A baseline recorded at
  home shows 100% AP change on every scan while driving.

The viable passive-presence alternative is BLE scanning — every phone with
BT on advertises, and a sustained unknown device at close RSSI is a real
signal. It is deliberately not shipped wired into the alarm: it needs its
own false-positive tuning. The hook point is `trigger_alert()`.

## Hardware lessons encoded here

These cost months of false alarms to learn; the comments in `cargate.py`
are the real documentation.

- **Percentage thresholds alone are not enough.** Normal driving
  fluctuations hit 16–20% on the stock sensitivity; the working values are
  a *50% change* AND an *800 µT absolute floor*, checked together. In a
  different magnetic environment (parking garage, next to a transformer)
  retune from your own logs.
- **Never re-baseline after an alert.** The original code reset its
  baseline to the current reading after every trigger — so each alert
  seeded the next one and a single bump became a cascade of dozens. The
  re-baseline line in `trigger_alert()` is disabled; keep it that way.
- **Cooldown is load-bearing.** 180 s between repeated alerts; shorter and
  one real event spams you out.
- **The proximity sensor is useless here.** It's a binary near-field
  "is a face near the earpiece" sensor; disabled by default.
- **Detached evidence capture.** `trigger_alert()` launches the capture
  hook with `start_new_session=True` — a sensor loop must never block on a
  multi-minute SSH/scp chain.
- **Journal timestamps lie under block buffering.** Python services
  buffer stdout; enable line buffering (`cargate.py` does it at startup)
  or your journald timeline is fiction.

## Topology

Two phones, no cloud:

```
                    LTE
   [alarm phone] ─── uplink ─── Telegram
        │  AP (NetworkManager shared, e.g. "car-ap")
        │
   [peripheral phone]  — Termux + sshd, camera + mic
```

The alarm phone is the router (LTE uplink + WiFi AP); the peripheral joins
the car SSID and is reachable by SSH. A dead peripheral degrades the alert
(no photo), never the alarm. The peripheral needs no SIM — evidence ships
out over the alarm phone's LTE.

`ap_manager.py` runs on a 5-minute timer as the away/home discriminator:
at home (alarm phone is a client of your home SSID) the car AP comes DOWN —
broadcasting a clone of your home SSID at home would hijack clients; away,
it comes UP so the peripheral can join.

## Install

Alarm phone (Ubuntu Touch; any stack exposing the sensor D-Bus service):

1. Install scripts into `~/alarm/bin/` and units into
   `~/.config/systemd/user/`.
2. `mkdir -p ~/.config/cargate && cp cargate.env ~/.config/cargate/env`
3. Fill in `TELEGRAM_BOT_TOKEN` and `CARGATE_CHAT_ID` at minimum.
4. `systemctl --user enable --now cargate.service ap-manager.timer
   evidence-flush.timer`

The daemon needs no root. `ap_manager.py` and the leases read in
`capture_hook.py` use `sudo -n` (configure NOPASSWD for `nmcli` and
`python3`, or run those parts another way).

Peripheral phone (Android with Termux):

1. Install Termux + `termux-api`, `openssh`, and a camera/mic permission
   grant; run `sshd` on port 8022.
2. Put `capture.sh` in `~/alarm/bin/`, install the alarm phone's SSH key.
3. Keep it awake: a wakelock or a charger. Android reaps daemons — that is
   why the *alarm brain* is the Ubuntu Touch phone and the peripheral is
   only ever a sensor slave.

Alert delivery needs a Telegram bot token (@BotFather) and your chat id
(@userinfobot). Verify live after configuring: a `curl` to
`.../sendMessage` must return 200 — a token with stripped quotes produces
401s that look like "Telegram is down".

## Files

- `cargate.py` — the alarm daemon (sensor watch, thresholds, alerting)
- `capture_hook.py` — SSH capture + pull + Telegram delivery + queue/flush
- `capture.sh` — runs *on the peripheral* (photos + audio via termux-api)
- `ap_manager.py` — car AP away/home discriminator
- `location_tracker.py` — WiFi/cell/IP location fingerprinting (optional)
- `cargate.service`, `ap-manager.{service,timer}`, `evidence-flush.{service,timer}`
- `cargate.env` — configuration template

## Testing

```bash
python3 tests/test_cargate.py
```

Offline checks, no device needed: config plumbing and env-file parsing, alert
templating and the cooldown state machine, sustained-offset re-baselining
(sustained steps re-baseline, oscillation does not), DHCP-lease resolution
with owner-phone exclusion, and the location tracker's directory layout.

## License

MIT — see LICENSE.