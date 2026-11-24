#!/usr/bin/env python3
"""Away/home discriminator for the car AP. Runs every 5 min (systemd timer).

- At home (alarm phone is a client of the home SSID): car AP must be DOWN.
  (Broadcasting a clone of the home SSID at home would hijack clients.)
- Away: car AP must be UP so the capture peripheral can join and be reachable.
"""
import subprocess
import sys
from types import SimpleNamespace

HOME_SSID = None  # set via CARGATE_HOME_SSID or the env file
AP_CONNECTION = None  # NetworkManager connection name of the car AP

def _load_env():
    global HOME_SSID, AP_CONNECTION
    import os
    HOME_SSID = os.environ.get("CARGATE_HOME_SSID", "")
    AP_CONNECTION = os.environ.get("CARGATE_AP_CONNECTION", "car-ap")
    env_file = os.path.expanduser(os.environ.get(
        "CARGATE_ENV_FILE", "~/.config/cargate/env"))
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CARGATE_HOME_SSID="):
                    HOME_SSID = line.split("=", 1)[1].strip()
                elif line.startswith("CARGATE_AP_CONNECTION="):
                    AP_CONNECTION = line.split("=", 1)[1].strip()
    except Exception:
        pass

_load_env()


def run(cmd, timeout=30):
    """subprocess.run that never raises: a missing/failed/timed-out probe
    reports returncode -1 so callers can tell ERROR from a clean negative."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return SimpleNamespace(returncode=-1, stdout="", stderr=str(e))


def home_client():
    """True at home, False away, None when the answer is UNKNOWN:
    HOME_SSID unset, or the nmcli probe failed/timed out. main() must
    refuse to touch the AP when this is None — mistaking a failed probe
    for "away" would raise the car AP at home (the hijack scenario)."""
    if not HOME_SSID:
        return None
    out = run(["nmcli", "-t", "f", "NAME,DEVICE,STATE", "c", "show", "--active"])
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        name = line.split(":")[0]
        if HOME_SSID in name and AP_CONNECTION not in name:
            return True
    return False


def main():
    if not HOME_SSID:
        print("CARGATE_HOME_SSID is not set — cannot tell home from away; "
              "refusing to manage the car AP. Set it in the env file or environment.")
        sys.exit(1)

    # Only act if the AP connection exists. A failed probe here is also a
    # no-action outcome — never act on a check you could not read.
    exists = run(["sudo", "-n", "nmcli", "connection", "show", AP_CONNECTION])
    if exists.returncode != 0:
        print(f"connection {AP_CONNECTION} does not exist yet — nothing to manage")
        sys.exit(0)

    home_client_active = home_client()
    if home_client_active is None:
        print("cannot determine home/away (nmcli probe failed) — leaving the AP alone")
        sys.exit(1)

    ap_probe = run(["nmcli", "-t", "-f", "NAME,DEVICE,STATE", "c", "show", "--active"])
    if ap_probe.returncode != 0:
        print("cannot read active connections — leaving the AP alone")
        sys.exit(1)
    ap_active = ap_probe.stdout

    if home_client_active and AP_CONNECTION in ap_active:
        # Should not happen: we're at home and clone-AP is up — bring it down NOW
        print(f"home + clone-AP active: bringing {AP_CONNECTION} DOWN")
        run(["sudo", "-n", "nmcli", "c", "down", AP_CONNECTION], timeout=90)
        sys.exit(0)

    if not home_client_active and AP_CONNECTION not in ap_active:
        # Away and no AP: bring it up so the peripheral can join
        print(f"away + no AP: bringing {AP_CONNECTION} UP")
        run(["sudo", "-n", "nmcli", "c", "up", AP_CONNECTION], timeout=180)
        sys.exit(0)

    print("state OK: home_client=%s ap_in_active=%s" % (home_client_active, AP_CONNECTION in ap_active))


if __name__ == "__main__":
    main()