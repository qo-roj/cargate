#!/usr/bin/env python3
"""Offline test suite for cargate — no device, no dbus, no network needed.

Stubs the dbus module and exercises everything that is not a live sensor
read: config plumbing, env-file parsing, alert templating, the cooldown
state machine, DHCP-lease resolution with owner-phone exclusion, and the
location tracker's directory layout.

Run: python3 tests/test_cargate.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="cargate-test-"))

os.environ["CARGATE_ALARM_DIR"] = str(WORK / "alarm")
os.environ["CARGATE_CAPTURE_ROOT"] = str(WORK / "captures")
os.environ["CARGATE_DEVICE_LABEL"] = "TestCar"
os.environ["CARGATE_ENV_FILE"] = str(WORK / "env")

# --- stub dbus: the real module needs a system bus; tests never touch sensors ---
fake_dbus = types.ModuleType("dbus")
class _FakeInterface:
    def __init__(self, *a, **k):
        pass
fake_dbus.Interface = _FakeInterface
class _FakeBus:
    def get_object(self, *a, **k):
        raise RuntimeError("no system bus in tests")
fake_dbus.SystemBus = _FakeBus
sys.modules["dbus"] = fake_dbus


def load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, str(REPO / relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- cargate.py: config + credentials ----------
cg = load("cargate_mod", "cargate.py")

assert cg.CONFIG["device_label"] == "TestCar"
assert cg.CONFIG["alarm_dir"] == str(WORK / "alarm")
assert cg.CONFIG["env_file"] == str(WORK / "env")

# token + chat id: env file wins when both are set (chargegate parity)
os.environ["TELEGRAM_BOT_TOKEN"] = "envtoken"
open(WORK / "env", "w").write('TELEGRAM_BOT_TOKEN="filetoken"\nCARGATE_CHAT_ID="42"\n')
tok, chat = cg.get_telegram_credentials()
assert tok == "filetoken" and chat == "42", (tok, chat)

# env var alone works when the file lacks the lines
open(WORK / "env", "w").write("")
tok, chat = cg.get_telegram_credentials()
assert tok == "envtoken" and chat == "", (tok, chat)
del os.environ["TELEGRAM_BOT_TOKEN"]
print("ok: cargate credentials (env var precedence, env file fallback)")

# ---------- cargate.py: alert templating + cooldown ----------
sent = []
cg.send_telegram_alert = lambda msg: sent.append(msg) or True
alarm = object.__new__(cg.CarAlarmDaemon)
alarm._alert_active = False
alarm._last_alert_time = 0
alarm._alert_cooldown = 180
alarm._get_location_context = lambda: None

alarm.trigger_alert(["Magnetometer: 900 units (55.0%, thr 50%)", "Light: 8000 lux"])
msg = sent[0]
assert "TESTCAR ALARM TRIGGERED" in msg, msg
assert "🚨 *CAR ALARM TRIGGERED" not in msg, msg   # un-templated fleet form
assert "Magnetometer: 900 units" in msg and "Light: 8000 lux" in msg

alarm.trigger_alert(["second trigger"])              # inside cooldown: suppressed
assert len(sent) == 1, "cooldown failed to suppress a re-trigger"
alarm._last_alert_time -= 200
alarm.trigger_alert(["third trigger"])               # outside: fires
assert len(sent) == 2 and "TESTCAR" in sent[1]
print("ok: alert templating + cooldown state machine")

# ---------- cargate.py: sustained-offset re-baseline logic (pure math) ----------
alarm._mag_threshold = 50.0
alarm._sustained_offset_start = None
alarm._sustained_offset_sign = 0
alarm._sustained_offset_rebaseline_s = 120
alarm._baseline = {"magnetometer": (0, 0, 0, 0, 100.0)}
rebased = []
alarm._read_all_sensors = lambda: rebased.append(1) or {"magnetometer": (0, 0, 0, 0, 200.0)}
import time as _time
alarm_time = [_time.time()]
orig_time = cg.time.time
cg.time.time = lambda: alarm_time[0]
try:
    # sustained +100% offset, direction stable -> re-baseline after 120s
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 200.0)})
    assert rebased == []
    alarm_time[0] += 130
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 200.0)})
    assert rebased == [1], "sustained offset did not re-baseline"
    assert alarm._baseline["magnetometer"][4] == 200.0
    # oscillating offset resets the tracker, never re-baselines
    alarm._sustained_offset_start = None
    alarm._baseline = {"magnetometer": (0, 0, 0, 0, 100.0)}
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 200.0)})
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 50.0)})   # flip
    alarm_time[0] += 130
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 50.0)})
    assert len(rebased) == 1, "oscillation should not re-baseline"
finally:
    cg.time.time = orig_time
print("ok: sustained-offset re-baseline (sustained yes, oscillation no)")

# ---------- ap_manager.py: empty config is safe ----------
ap = load("ap_manager_mod", "ap_manager.py")
assert ap.HOME_SSID == "" and ap.AP_CONNECTION == "car-ap"
assert ap.home_client() is False   # no SSID configured -> no nmcli, no crash
print("ok: ap_manager empty-config safety")

# ---------- capture_hook.py: lease resolution + owner exclusion ----------
ch = load("capture_hook_mod", "capture_hook.py")
assert ch.CONFIG["peripheral_port"] == 8022
assert ch.CONFIG["owner_host_prefixes"] == []
assert ch.PENDING_DIR == str(WORK / "captures" / "pending")

LEASES = ("1791817679 56:df:99:e7:c9:34 10.1.0.157 peripheral 01:x\n"
          "1791817999 56:df:99:e7:c9:35 10.1.0.158 OWNPHONE-X7 01:y\n")
def fake_run(cmd, **kw):
    r = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if "python3" in cmd and "-c" in cmd:
        r.stdout = LEASES
    return r
ch.subprocess.run = fake_run

ch.CONFIG["owner_host_prefixes"] = ["OWNPHONE-X7"]
assert ch.resolve_peripheral_ip() == "10.1.0.157", "owner phone must be excluded"
ch.CONFIG["owner_host_prefixes"] = []
assert ch.resolve_peripheral_ip() == "10.1.0.158", "newest lease wins"
assert ch.resolve_peripheral_ip() is not None
# empty leases file -> None, no crash
LEASES = ""
assert ch.resolve_peripheral_ip() is None
print("ok: capture_hook lease resolution + owner-phone exclusion")

# ---------- location_tracker.py: env-var dir layout ----------
lt = load("location_tracker_mod", "location_tracker.py")
assert lt.BASE_DIR == Path(str(WORK / "alarm"))
assert lt.DB_DIR.exists() and lt.LOG_DIR.exists()
print("ok: location_tracker CARGATE_ALARM_DIR honored")

# ---------- capture.sh: bash syntax ----------
r = subprocess.run(["bash", "-n", str(REPO / "capture.sh")],
                   capture_output=True, text=True)
assert r.returncode == 0, r.stderr
print("ok: capture.sh bash -n")

print("\nALL TESTS PASSED")