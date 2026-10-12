#!/usr/bin/env python3
"""Away/home discriminator for the car AP. Runs every 5 min (systemd timer).

- At home (alarm phone is a client of the home SSID): car AP must be DOWN.
  (Broadcasting a clone of the home SSID at home would hijack clients.)
- Away: car AP must be UP so the capture peripheral can join and be reachable.
"""
import subprocess
import sys

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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def home_client():
    """True if the alarm phone is currently a client of the home SSID."""
    if not HOME_SSID:
        return False
    out = run(["nmcli", "-t", "f", "NAME,DEVICE,STATE", "c", "show", "--active"])
    for line in out.stdout.splitlines():
        name = line.split(":")[0]
        if HOME_SSID in name and AP_CONNECTION not in name:
            return True
    return False


def main():
    # Only act if the AP connection exists
    exists = run(["sudo", "-n", "nmcli", "connection", "show", AP_CONNECTION])
    if exists.returncode != 0:
        print(f"connection {AP_CONNECTION} does not exist yet — nothing to manage")
        sys.exit(0)

    home_client_active = home_client()
    ap_active = run(["nmcli", "-t", "-f", "NAME,DEVICE,STATE", "c", "show", "--active"]).stdout

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