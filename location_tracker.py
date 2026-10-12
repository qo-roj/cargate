#!/usr/bin/env python3
"""
location_tracker.py — WiFi fingerprint + cell/IP geolocation for the car alarm.
Runs on Ubuntu Touch. No GPS required.
Uses: nmcli, busctl (ofono), curl (ip-api.com).
"""

import json
import dbus
import os
import re
import subprocess
import time
import sys
from pathlib import Path

BASE_DIR = Path(os.environ.get("CARGATE_ALARM_DIR", Path.home() / "alarm")).expanduser()
DB_DIR = BASE_DIR / "db"
LOG_DIR = BASE_DIR / "log"
DB_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

BASELINE_PATH = DB_DIR / "wifi_baseline.json"
STATE_PATH = DB_DIR / "location_state.json"
LOG_PATH = LOG_DIR / "location.log"

# Tunables
SIG_SHIFT_DB = 15          # dBm change in a single AP to flag movement
JACCARD_MIN = 0.5          # minimum overlap fraction of top-6 BSSIDs
TOP_N = 6                  # how many APs to fingerprint
SAMPLE_INTERVAL_S = 60     # how often to sample


def log(msg):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), 1


def wifi_scan():
    """Return list of {bssid, signal, ssid} sorted by signal desc."""
    out, err, code = run("nmcli -t --escape no -f BSSID,SIGNAL,SSID device wifi list 2>/dev/null")
    if code != 0:
        log(f"WiFi scan failed: {err}")
        return []
    nets = []
    for line in out.splitlines():
        if not line or line.startswith("IN-USE"):
            continue
        # nmcli -t uses : as field separator; BSSID contains colons.
        # Some versions prefix the connected AP with *. Strip it first.
        line = line.lstrip("*")
        # Robust parse: match BSSID (6 hex pairs), signal (int), then the rest is SSID
        m = re.match(r'^([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}):(-?\d+):(.*)$', line)
        if not m:
            continue
        bssid, signal_str, ssid = m.groups()
        try:
            signal = int(signal_str)
        except ValueError:
            continue
        nets.append({"bssid": bssid.lower(), "signal": signal, "ssid": ssid})
    # sort by signal desc
    nets.sort(key=lambda x: x["signal"], reverse=True)
    # dedupe by BSSID keeping strongest
    seen = set()
    uniq = []
    for n in nets:
        if n["bssid"] not in seen:
            seen.add(n["bssid"])
            uniq.append(n)
    return uniq


def take_baseline():
    nets = wifi_scan()
    baseline = {
        "ts": time.time(),
        "aps": nets[:TOP_N],
    }
    with open(BASELINE_PATH, "w") as f:
        json.dump(baseline, f, indent=2)
    log(f"Baseline saved: {len(baseline['aps'])} APs")
    return baseline


def load_baseline():
    if not BASELINE_PATH.exists():
        return None
    with open(BASELINE_PATH) as f:
        return json.load(f)


def wifi_delta(current, baseline):
    """Compare current scan to baseline. Returns (moved:bool, details:str)."""
    if not baseline or not baseline.get("aps"):
        return True, "no baseline"
    base_aps = {a["bssid"]: a for a in baseline["aps"]}
    cur_aps = {a["bssid"]: a for a in current[:TOP_N]}

    # Jaccard overlap
    common = set(base_aps.keys()) & set(cur_aps.keys())
    union = set(base_aps.keys()) | set(cur_aps.keys())
    jaccard = len(common) / len(union) if union else 0.0

    if jaccard < JACCARD_MIN:
        return True, f"Jaccard={jaccard:.2f} (<{JACCARD_MIN})"

    # Signal drift of common APs
    max_shift = 0
    shifted = []
    for bssid in common:
        shift = abs(cur_aps[bssid]["signal"] - base_aps[bssid]["signal"])
        if shift > max_shift:
            max_shift = shift
        if shift > SIG_SHIFT_DB:
            shifted.append(f"{bssid[:8]}.. {shift}dBm")

    if shifted:
        return True, f"signal shift >{SIG_SHIFT_DB}dBm: {', '.join(shifted)}"

    return False, f"Jaccard={jaccard:.2f} max_shift={max_shift}dBm"


def cell_info():
    """Query ofono cell tower info via D-Bus GetProperties."""
    try:
        bus = dbus.SystemBus()
        obj = bus.get_object("org.ofono", "/ril_1")
        iface = dbus.Interface(obj, "org.ofono.NetworkRegistration")
        props = iface.GetProperties()
        if not props:
            return None
        return {
            "CellId": str(props.get("CellId", "")),
            "MobileCountryCode": str(props.get("MobileCountryCode", "")),
            "MobileNetworkCode": str(props.get("MobileNetworkCode", "")),
            "Technology": str(props.get("Technology", "")),
            "Strength": str(props.get("Strength", "")),
            "Name": str(props.get("Name", "")),
        }
    except Exception:
        return None


def ip_geolocation():
    """Query ip-api.com for coarse lat/lon."""
    url = 'https://ip-api.com/json/?fields=status,lat,lon,query,country,city'
    out, err, code = run(f"curl -s --max-time 5 '{url}' 2>/dev/null")
    if 0 != code:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def location_snapshot():
    """Full snapshot: WiFi + cell + IP."""
    snap = {
        "ts": time.time(),
        "wifi": wifi_scan()[:TOP_N],
        "cell": cell_info(),
        "ipgeo": ip_geolocation(),
    }
    return snap


def check_and_log():
    baseline = load_baseline()
    if not baseline:
        log("No baseline found; arming now.")
        baseline = take_baseline()
    snap = location_snapshot()
    moved, reason = wifi_delta(snap["wifi"], baseline)

    # persist latest state
    with open(STATE_PATH, "w") as f:
        json.dump(snap, f, indent=2)

    wifi_desc = ",".join([f"{a['bssid'][:8]}({a['signal']})" for a in snap["wifi"][:3]])
    cell_desc = "-"
    if snap["cell"]:
        cell_desc = f"{snap['cell'].get('Name','?')} cid={snap['cell'].get('CellId','?')} tech={snap['cell'].get('Technology','?')}"
    geo_desc = "-"
    if snap["ipgeo"] and snap["ipgeo"].get("status") == "success":
        geo_desc = f"{snap['ipgeo'].get('lat','?')},{snap['ipgeo'].get('lon','?')} {snap['ipgeo'].get('city','')}"

    status = "MOVED" if moved else "STABLE"
    log(f"{status} | wifi=[{wifi_desc}] | cell=[{cell_desc}] | geo=[{geo_desc}] | reason={reason}")
    return moved, snap, reason


def arm():
    """Set current location as the safe baseline."""
    log("ARMING — taking new WiFi baseline")
    return take_baseline()


def main_loop():
    log("location_tracker starting")
    baseline = load_baseline()
    if not baseline:
        log("No baseline found; arming now.")
        take_baseline()

    while True:
        try:
            moved, snap, reason = check_and_log()
            if moved:
                # The car alarm poller can read STATE_PATH to decide
                pass
        except Exception as e:
            log(f"ERROR in main_loop: {e}")
        time.sleep(SAMPLE_INTERVAL_S)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Location tracker (WiFi/cell/IP)")
    parser.add_argument("--arm", action="store_true", help="Snapshot current location as safe baseline")
    parser.add_argument("--check-once", action="store_true", help="Run one check and exit")
    parser.add_argument("--daemon", action="store_true", help="Loop forever checking location")
    args = parser.parse_args()

    if args.arm:
        arm()
    elif args.check_once:
        moved, snap, reason = check_and_log()
        sys.exit(1 if moved else 0)
    elif args.daemon:
        main_loop()
    else:
        parser.print_help()