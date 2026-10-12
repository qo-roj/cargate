#!/usr/bin/env python3
"""Evidence-capture hook: SSH into the peripheral phone, pull a photo/audio
burst, deliver to Telegram. Called from cargate.py trigger_alert().

Usage:
  capture_hook.py [--ip <peripheral-ip>] [--reason "magnetometer 55%"]
  capture_hook.py --flush    (retry queued evidence; only useful on fast uplink)

Throttle reality: on a metered LTE uplink past its data plan, sendMessage (text)
still goes through; photo uploads do not. So: captures are always taken and
pulled; Telegram photo sends get a short timeout; anything that fails lands in
the pending/ queue and the flush timer delivers it later.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

CONFIG = {
    "env_file": os.path.expanduser(os.environ.get(
        "CARGATE_ENV_FILE", "~/.config/cargate/env")),
    "chat_id": os.environ.get("CARGATE_CHAT_ID", ""),
    "peripheral_user": os.environ.get("CARGATE_PERIPHERAL_USER", ""),
    "peripheral_port": int(os.environ.get("CARGATE_PERIPHERAL_PORT", "8022")),
    "ssh_key": os.path.expanduser(os.environ.get(
        "CARGATE_SSH_KEY", "~/.ssh/id_ed25519")),
    "leases_file": os.environ.get(
        "CARGATE_LEASES_FILE",
        "/var/lib/NetworkManager/dnsmasq-wlan0.leases"),
    "remote_capture": os.environ.get(
        "CARGATE_REMOTE_CAPTURE", "bash ~/alarm/bin/capture.sh"),
    # hostname prefixes (comma-separated) of household phones that must
    # never be mistaken for the capture peripheral
    "owner_host_prefixes": [
        p.strip() for p in os.environ.get("CARGATE_OWNER_HOST_PREFIXES", "").split(",")
        if p.strip()],
    "home_ssid": os.environ.get("CARGATE_HOME_SSID", ""),
    "ap_connection": os.environ.get("CARGATE_AP_CONNECTION", "car-ap"),
    "capture_root": os.path.expanduser(os.environ.get(
        "CARGATE_CAPTURE_ROOT", "~/captures")),
}
PENDING_DIR = os.path.join(CONFIG["capture_root"], "pending")
TELEGRAM = "https://api.telegram.org/bot{}/{}"
SEND_TIMEOUT = 25  # short: throttled LTE hangs instead of failing

SUDO = ["sudo", "-n"]


def get_token():
    """Bot token: env var first, then the env file."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    try:
        with open(CONFIG["env_file"]) as f:
            for line in f:
                m = re.match(r"TELEGRAM_BOT_TOKEN=(.+)", line.strip())
                if m:
                    token = m.group(1).strip().strip('"')
                    break
    except Exception:
        pass
    return token


def get_chat_id():
    """Chat id: env var first, then the env file."""
    chat_id = CONFIG["chat_id"]
    try:
        with open(CONFIG["env_file"]) as f:
            for line in f:
                m = re.match(r"CARGATE_CHAT_ID=(.+)", line.strip())
                if m:
                    chat_id = m.group(1).strip().strip('"')
                    break
    except Exception:
        pass
    return chat_id


def resolve_peripheral_ip():
    """Newest DHCP lease that is not an owner phone."""
    try:
        out = subprocess.run(
            SUDO + ["python3", "-c",
                    "print(open('%s').read())" % CONFIG["leases_file"]],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None
    candidates = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            exp, mac, ip, host = parts[0], parts[1], parts[2], parts[3]
            if any(host.startswith(p) for p in CONFIG["owner_host_prefixes"]):
                continue  # an owner's phone, never the capture peripheral
            candidates.append((int(exp), ip, host))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def ssh_peripheral(ip, cmd, timeout=90):
    return subprocess.run(
        ["ssh", "-i", CONFIG["ssh_key"], "-p", str(CONFIG["peripheral_port"]),
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
         f"{CONFIG['peripheral_user']}@{ip}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def telegram_send(method, token, **kwargs):
    """Minimal multipart sender, stdlib only. Returns True on success."""
    boundary = uuid.uuid4().hex
    body = b""
    fields = {}
    files = {}
    for k, v in kwargs.items():
        if isinstance(v, tuple):
            files[k] = v
        else:
            fields[k] = str(v)
    for k, v in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (fname, data, ctype) in files.items():
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        TELEGRAM.format(token, method), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as r:
            resp = json.loads(r.read().decode())
            return resp.get("ok", False)
    except Exception as e:
        print(f"telegram {method} failed: {e}", file=sys.stderr)
        return False


def queue_file(path, reason):
    """Move a failed-send evidence file into pending/ (flat, timestamp-prefixed)."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c for c in reason[:24] if c.isalnum() or c in " _-").replace(" ", "_")
    dest = os.path.join(PENDING_DIR, f"{stamp}_{safe}_{os.path.basename(path)}")
    shutil.move(path, dest)
    return dest


def flush_pending(token):
    """Retry queued evidence. Returns (sent, remaining)."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    sent, remaining = 0, 0
    for p in sorted(glob.glob(os.path.join(PENDING_DIR, "*"))):
        base = os.path.basename(p)
        data = open(p, "rb").read()
        if base.endswith((".jpg", ".jpeg")):
            ok = telegram_send("sendPhoto", token, chat_id=get_chat_id(),
                                caption=f"Queued evidence: {base}",
                                photo=(base, data, "image/jpeg"))
        elif base.endswith(".m4a"):
            ok = telegram_send("sendDocument", token, chat_id=get_chat_id(),
                               caption=f"Queued evidence: {base}",
                               document=(base, data, "audio/mp4"))
        else:
            ok = telegram_send("sendDocument", token, chat_id=get_chat_id(),
                               caption=f"Queued evidence: {base}",
                               document=(base, data, "application/octet-stream"))
        if ok:
            os.remove(p)
            sent += 1
        else:
            remaining += 1
    return sent, remaining


def on_home_wifi():
    """True if the alarm phone is a client of the home SSID (fast uplink)."""
    if not CONFIG["home_ssid"]:
        return False
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "c", "show", "--active"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            name = line.split(":")[0]
            if CONFIG["home_ssid"] in name and CONFIG["ap_connection"] not in name:
                return True
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip")
    ap.add_argument("--reason", default="alarm trigger")
    ap.add_argument("--flush", action="store_true",
                    help="retry pending evidence queue")
    args = ap.parse_args()

    token = get_token()

    if args.flush:
        if not token:
            print("no token; cannot flush", file=sys.stderr)
            sys.exit(1)
        if not on_home_wifi():
            print("not on home WiFi — skipping flush (slow uplink)")
            sys.exit(0)
        sent, remaining = flush_pending(token)
        print(f"flush: sent={sent} remaining={remaining}")
        if sent > 0 and remaining == 0:
            telegram_send("sendMessage", token, chat_id=get_chat_id(),
                          text=f"✅ Evidence queue flushed: {sent} files delivered")
        sys.exit(0)

    if not token:
        print("NO TOKEN — capture will proceed but Telegram will fail", file=sys.stderr)

    ip = args.ip or resolve_peripheral_ip()
    if not ip:
        print("no peripheral lease found in DHCP leases", file=sys.stderr)
        if token:
            telegram_send("sendMessage", token, chat_id=get_chat_id(),
                          text="⚠️ Alarm trigger, but the capture peripheral is not on the car AP — no photo evidence. Reason: " + args.reason)
        sys.exit(1)

    print(f"peripheral at {ip}, triggering capture...")
    r = ssh_peripheral(ip, CONFIG["remote_capture"], timeout=150)
    if r.returncode != 0:
        print(f"capture ssh failed: {r.stderr}", file=sys.stderr)
        if token:
            telegram_send("sendMessage", token, chat_id=get_chat_id(),
                          text=f"⚠️ Alarm trigger ({args.reason}); peripheral capture FAILED: {r.stderr.strip()[:200]}")
        sys.exit(1)

    outdir = None
    files = []
    for line in r.stdout.splitlines():
        if line.startswith("DIR "):
            outdir = line[4:].strip()
        elif line.strip():
            files.append(line.strip())

    if not outdir or not files:
        print("capture produced no files", file=sys.stderr)
        if token:
            telegram_send("sendMessage", token, chat_id=get_chat_id(),
                          text=f"⚠️ Alarm trigger ({args.reason}); peripheral capture returned no files")
        sys.exit(1)

    ts = time.strftime("%Y%m%d-%H%M%S")
    local_dir = os.path.join(CONFIG["capture_root"], ts)
    os.makedirs(local_dir, exist_ok=True)

    pulled = []
    for f in files:
        scp = subprocess.run(
            ["scp", "-i", CONFIG["ssh_key"], "-P", str(CONFIG["peripheral_port"]),
             "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
             f"{CONFIG['peripheral_user']}@{ip}:{f}", os.path.join(local_dir, os.path.basename(f))],
            capture_output=True, text=True, timeout=180,
        )
        if scp.returncode == 0:
            pulled.append(os.path.join(local_dir, os.path.basename(f)))
        else:
            print(f"scp {f} failed: {scp.stderr}", file=sys.stderr)

    print(f"pulled {len(pulled)} files to {local_dir}")
    if not pulled:
        sys.exit(1)

    # Downscale photos for upload
    sendable = []
    for p in sorted(glob.glob(os.path.join(local_dir, "cam*.jpg"))):
        small = os.path.join(local_dir, "s_" + os.path.basename(p))
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", p,
             "-vf", "scale=1280:-2", "-q:v", "5", small],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0 and os.path.exists(small):
            sendable.append(small)
        else:
            print(f"ffmpeg downscale failed for {p}: {r.stderr}", file=sys.stderr)
            sendable.append(p)

    queued = 0
    sent = 0
    if token:
        for p in sendable:
            with open(p, "rb") as fh:
                if telegram_send("sendPhoto", token, chat_id=get_chat_id(),
                                 caption=f"Car alarm capture — {args.reason}",
                                 photo=(os.path.basename(p), fh.read(), "image/jpeg")):
                    sent += 1
                else:
                    queue_file(p, args.reason)
                    queued += 1
            time.sleep(1)
        audio = os.path.join(local_dir, "audio.m4a")
        if os.path.exists(audio):
            with open(audio, "rb") as fh:
                if telegram_send("sendDocument", token, chat_id=get_chat_id(),
                                 caption=f"Audio (10s) — {args.reason}",
                                 document=("audio.m4a", fh.read(), "audio/mp4")):
                    sent += 1
                else:
                    queue_file(audio, args.reason)
                    queued += 1

        status = (f"✅ Peripheral capture: {len(pulled)} files pulled"
                  f" ({sent} sent, {queued} queued for home-WiFi flush). "
                  f"Trigger: {args.reason}. Peripheral at {ip}")
        telegram_send("sendMessage", token, chat_id=get_chat_id(), text=status)

    print(f"done: sent={sent} queued={queued}")


if __name__ == "__main__":
    main()