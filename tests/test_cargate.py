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
real_subprocess_run = subprocess.run   # ch.subprocess IS this module; stubs
                                       # poison it globally — keep the real one


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
real_send_telegram_alert = cg.send_telegram_alert   # saved before any stubbing

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
alarm._alert_fail_streak = 0
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
alarm._mag_absolute_min = 800.0
alarm._sustained_offset_start = None
alarm._sustained_offset_sign = 0
alarm._sustained_offset_rebaseline_s = 120
alarm._baseline = {"magnetometer": (0, 0, 0, 0, 1000.0)}
rebased = []
alarm._read_all_sensors = lambda: rebased.append(1) or {"magnetometer": (0, 0, 0, 0, 2000.0)}
import time as _time
alarm_time = [_time.time()]
orig_time = cg.time.time
cg.time.time = lambda: alarm_time[0]
try:
    # sustained +100% offset above BOTH gates, direction stable -> re-baseline
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 2000.0)})
    assert rebased == []
    alarm_time[0] += 130
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 2000.0)})
    assert rebased == [1], "sustained offset did not re-baseline"
    assert alarm._baseline["magnetometer"][4] == 2000.0
    # sub-floor drift (60% pct but only 600 units < 800 abs) must NOT
    # re-baseline: it can never alert, so absorbing it silently would let a
    # slow magnet ratchet the baseline without a single alarm firing
    alarm._sustained_offset_start = None
    alarm._baseline = {"magnetometer": (0, 0, 0, 0, 1000.0)}
    alarm_time[0] += 130
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 1600.0)})
    assert len(rebased) == 1, "sub-floor sustained drift must not re-baseline"
    # oscillating offset resets the tracker, never re-baselines
    alarm._sustained_offset_start = None
    alarm._baseline = {"magnetometer": (0, 0, 0, 0, 1000.0)}
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 2000.0)})
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 500.0)})   # flip
    alarm_time[0] += 130
    alarm._check_sustained_offset({"magnetometer": (0, 0, 0, 0, 500.0)})
    assert len(rebased) == 1, "oscillation should not re-baseline"
finally:
    cg.time.time = orig_time
print("ok: sustained-offset re-baseline (sustained yes, oscillation no)")

# ---------- ap_manager.py: unset SSID or failed probe = refuse to act ----------
ap = load("ap_manager_mod", "ap_manager.py")
assert ap.HOME_SSID == "" and ap.AP_CONNECTION == "car-ap"
assert ap.home_client() is None   # SSID unset -> UNKNOWN, never "away"
ap_calls = []
def ap_run_recorder(cmd, timeout=30):
    ap_calls.append(cmd)
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
ap.run = ap_run_recorder
try:
    ap.main()
    assert False, "main() must refuse to act with HOME_SSID unset"
except SystemExit as e:
    assert e.code == 1, e.code
assert ap_calls == [], "the refusal must not touch nmcli at all"
# a FAILED probe must also refuse: a "not home" misread raises the AP at home
ap.HOME_SSID = "HomeNet"
def ap_run_mixed(cmd, timeout=30):
    if "connection" in " ".join(cmd):
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return types.SimpleNamespace(returncode=-1, stdout="", stderr="probe died")
ap.run = ap_run_mixed
try:
    ap.main()
    assert False, "main() must refuse when the home/away probe fails"
except SystemExit as e:
    assert e.code == 1, e.code
print("ok: ap_manager refuses to act on unset SSID or failed probe")

# ---------- capture_hook.py: lease resolution + owner exclusion ----------
ch = load("capture_hook_mod", "capture_hook.py")
assert ch.CONFIG["peripheral_port"] == 8022
assert ch.CONFIG["owner_host_prefixes"] == []
assert ch.PENDING_DIR == str(WORK / "captures" / "pending")

LEASES = ("# a comment-looking line\n"
          "not-a-lease\n"
          "duetime 56:df:99:e7:c9:36 10.1.0.159 rogue 01:z\n"   # non-numeric expiry
          "1791817679 56:df:99:e7:c9:34 10.1.0.157 peripheral 01:x\n"
          "1791817999 56:df:99:e7:c9:35 10.1.0.158 OWNPHONE-X7 01:y\n")
lease_calls = []
def fake_run(cmd, **kw):
    r = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if "python3" in cmd and "-c" in cmd:
        lease_calls.append(list(cmd))
        r.stdout = LEASES
    return r
ch.subprocess.run = fake_run

ch.CONFIG["owner_host_prefixes"] = ["OWNPHONE-X7"]
assert ch.resolve_peripheral_ip() == "10.1.0.157", "owner phone must be excluded"
ch.CONFIG["owner_host_prefixes"] = []
assert ch.resolve_peripheral_ip() == "10.1.0.158", "newest lease wins"
assert ch.resolve_peripheral_ip() is not None
# the leases path rides argv — never interpolated into the python -c code
last_call = lease_calls[-1]
assert str(ch.CONFIG["leases_file"]) in last_call, "leases path must be its own argv element"
assert not any("'" in c and "open(" in c for c in last_call), "no path spliced into the code string"
# empty leases file -> None, no crash
LEASES = ""
assert ch.resolve_peripheral_ip() is None
print("ok: capture_hook leases: malformed-line tolerance, owner exclusion, argv path")

# ---------- location_tracker.py: env-var dir layout ----------
lt = load("location_tracker_mod", "location_tracker.py")
assert lt.BASE_DIR == Path(str(WORK / "alarm"))
assert lt.DB_DIR.exists() and lt.LOG_DIR.exists()
print("ok: location_tracker CARGATE_ALARM_DIR honored")

# ---------- capture.sh: bash syntax ----------
r = real_subprocess_run(["bash", "-n", str(REPO / "capture.sh")],
                        capture_output=True, text=True)
assert r.returncode == 0, r.stderr
print("ok: capture.sh bash -n")

# ---------- cargate.py: cooldown stamps only on SUCCESSFUL sends ----------
sent2 = []
alarm2 = object.__new__(cg.CarAlarmDaemon)
alarm2._alert_active = False
alarm2._last_alert_time = 0
alarm2._alert_cooldown = 180
alarm2._alert_fail_streak = 0
alarm2._get_location_context = lambda: None
t2 = [_time.time()]
cg.time.time = lambda: t2[0]
try:
    cg.send_telegram_alert = lambda msg: sent2.append(msg) or False   # Telegram down
    alarm2.trigger_alert(["fail send"])
    assert len(sent2) == 1
    t2[0] += 30    # still inside the 60s failure backoff
    alarm2.trigger_alert(["fail send"])
    assert len(sent2) == 1, "failed send must not retry before its backoff"
    t2[0] += 31    # 61s after the failed send -> retry allowed (streak 2 -> 120s backoff)
    alarm2.trigger_alert(["fail send"])
    assert len(sent2) == 2, "failed send must retry after its backoff"
    cg.send_telegram_alert = lambda msg: sent2.append(msg) or True    # Telegram back
    t2[0] += 120   # streak-2 backoff elapsed
    alarm2.trigger_alert(["ok send"])
    assert len(sent2) == 3
    t2[0] += 179   # inside the full 180s success cooldown
    alarm2.trigger_alert(["ok send"])
    assert len(sent2) == 3, "a successful send must apply the full cooldown"
    t2[0] += 2
    alarm2.trigger_alert(["ok send"])
    assert len(sent2) == 4
finally:
    cg.time.time = orig_time
print("ok: cooldown stamps on success; failed sends back off and retry")

# ---------- cargate.py: Telegram 200 + ok:false counts as failure ----------
class _FakeResp:
    def __init__(self, body):
        self._b = body
    def read(self):
        return self._b
_orig_urlopen = cg.urllib.request.urlopen
os.environ["TELEGRAM_BOT_TOKEN"] = "testtok"
try:
    cg.urllib.request.urlopen = lambda req, timeout=None: _FakeResp(b'{"ok": false, "description": "chat not found"}')
    assert real_send_telegram_alert("x") is False, "ok:false must be a failure"
    cg.urllib.request.urlopen = lambda req, timeout=None: _FakeResp(b'{"ok": true, "result": {}}')
    assert real_send_telegram_alert("x") is True, "ok:true must be a success"
finally:
    cg.urllib.request.urlopen = _orig_urlopen
    del os.environ["TELEGRAM_BOT_TOKEN"]
print("ok: Telegram ok:false parsed as a failed send")

# ---------- cargate.py: arm failure exits non-zero (systemd must revive) ----------
runner = (
    "import sys, types\n"
    "fake = types.ModuleType('dbus')\n"
    "class _FI:\n"
    "    def __init__(self, *a, **k): pass\n"
    "class _FO: pass\n"
    "class _FB:\n"
    "    def get_object(self, *a, **k): return _FO()\n"
    "fake.Interface = _FI\n"
    "fake.SystemBus = _FB\n"
    "sys.modules['dbus'] = fake\n"
    "sys.argv = ['cargate.py']\n"
    "import runpy\n"
    "runpy.run_path(%r, run_name='__main__')\n"
) % (str(REPO / "cargate.py"),)
open(WORK / "runner.py", "w").write(runner)
r = real_subprocess_run([sys.executable, str(WORK / "runner.py")],
                        capture_output=True, text=True, timeout=120)
assert r.returncode == 1, f"arm failure must exit 1, got {r.returncode}\n{r.stdout[-400:]}\n{r.stderr[-400:]}"
assert "Failed to arm" in r.stdout
print("ok: arm failure exits non-zero (Restart=on-failure revives the daemon)")

# ---------- cargate.py: config keys honor the env file (file wins) ----------
open(WORK / "env2", "w").write("CARGATE_DEVICE_LABEL=FileCar\n")
os.environ["CARGATE_ENV_FILE"] = str(WORK / "env2")
cg2 = load("cargate_mod2", "cargate.py")
assert cg2.CONFIG["device_label"] == "FileCar", "env-file CARGATE_DEVICE_LABEL must win"
del os.environ["CARGATE_ENV_FILE"]
print("ok: cargate config reads the env file (file wins over environment)")

# ---------- location_tracker: ip-api over HTTP, cached incl. failures ----------
lt_calls = []
def lt_run_ok(cmd, timeout=15):
    lt_calls.append(cmd)
    return ('{"status":"success","lat":1.0,"lon":2.0,"city":"X"}', "", 0)
lt.run = lt_run_ok
lt_time = [_time.time()]
_orig_lt_time = lt.time.time
lt.time.time = lambda: lt_time[0]
try:
    v1 = lt.ip_geolocation()
    v2 = lt.ip_geolocation()
    assert len(lt_calls) == 1 and v1 is v2 and v1["status"] == "success"
    assert "http://" in lt_calls[0] and "https://" not in lt_calls[0], "free tier is HTTP-only"
    lt_time[0] += 1801   # TTL expired -> fresh probe
    lt.ip_geolocation()
    assert len(lt_calls) == 2
    lt_fail_calls = []
    lt.run = lambda cmd, timeout=15: (lt_fail_calls.append(cmd) or ("", "curl died", 1))
    lt_time[0] += 1801   # success cache expires -> re-probe, which now FAILS
    assert lt.ip_geolocation() is None
    assert len(lt_fail_calls) == 1, "expired success cache must re-probe (and fail)"
    lt_time[0] += 100
    assert lt.ip_geolocation() is None
    assert len(lt_fail_calls) == 1, "a fresh failure must come from the cache"
    lt_time[0] += 1801
    assert lt.ip_geolocation() is None
    assert len(lt_fail_calls) == 2, "an expired failure cache must re-probe"
finally:
    lt.time.time = _orig_lt_time
print("ok: ip-api uses HTTP (free tier) and caches successes and failures")

# ---------- capture_hook: flush skips directories; all-scp-fail notifies ----------
os.makedirs(ch.PENDING_DIR, exist_ok=True)
os.makedirs(os.path.join(ch.PENDING_DIR, "stray_dir"), exist_ok=True)
open(os.path.join(ch.PENDING_DIR, "queued.jpg"), "wb").write(b"\xff\xd8fakejpg")
_flush_sent = []
ch.telegram_send = lambda method, token, **kw: _flush_sent.append(method) or True
s, rem = ch.flush_pending("tok")
assert (s, rem) == (1, 0), (s, rem)
assert _flush_sent == ["sendPhoto"], _flush_sent
import shutil as _shutil
_shutil.rmtree(ch.PENDING_DIR)
print("ok: evidence flush skips stray directories")

ch2 = load("capture_hook_mod2", "capture_hook.py")
ch2.get_token = lambda: "tok"
ch2.get_chat_id = lambda: "42"
ch2.resolve_peripheral_ip = lambda: "10.1.0.5"
ch2.ssh_peripheral = lambda ip, cmd, timeout=90: types.SimpleNamespace(
    returncode=0, stdout="DIR /remote/out\n/remote/out/cam0.jpg", stderr="")
_scp_sent = []
ch2.telegram_send = lambda method, token, **kw: _scp_sent.append((method, kw.get("text", ""))) or True
def _failing_scp(cmd, **kw):
    return types.SimpleNamespace(returncode=1, stdout="", stderr="scp boom")
ch2.subprocess.run = _failing_scp
_argv = sys.argv
sys.argv = ["capture_hook.py", "--reason", "suite"]
try:
    try:
        ch2.main()
        assert False, "all-scp-fail must exit 1"
    except SystemExit as e:
        assert e.code == 1, e.code
    texts = [t for m, t in _scp_sent]
    assert any("pulled 0 files" in t for t in texts), texts
finally:
    sys.argv = _argv
_shutil.rmtree(ch2.CONFIG["capture_root"], ignore_errors=True)
print("ok: an all-scp-fail capture still sends a Telegram warning")

print("\nALL TESTS PASSED")