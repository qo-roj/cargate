#!/usr/bin/env python3
"""
cargate — car alarm daemon for Ubuntu Touch Linux phones, using D-Bus sensors.
Monitors magnetometer, light, orientation, and proximity sensors
to detect vehicle tampering or movement.
"""
import subprocess
import dbus
import time
import math
import os
import signal
import json
import sys
import urllib.request
import urllib.parse
import argparse
from pathlib import Path
from typing import Dict, Optional, Tuple, Any, List

CONFIG = {
    "env_file": os.path.expanduser(os.environ.get(
        "CARGATE_ENV_FILE", "~/.config/cargate/env")),
    "telegram_chat_id": os.environ.get("CARGATE_CHAT_ID", ""),
    "device_label": os.environ.get("CARGATE_DEVICE_LABEL", "car"),
    "alarm_dir": os.path.expanduser(os.environ.get("CARGATE_ALARM_DIR", "~/alarm")),
    # evidence-capture hook launched on trigger; resolved next to this file
    # by default. Set CARGATE_CAPTURE_SCRIPT to an absolute path to override.
    "capture_script": os.path.expanduser(os.environ.get("CARGATE_CAPTURE_SCRIPT", "")),
}
PID_FILE = Path(CONFIG["alarm_dir"]) / "logs" / "cargate.pid"

def get_telegram_credentials():
    """Bot token + chat id: env vars first, then the env file."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = CONFIG["telegram_chat_id"]
    try:
        with open(CONFIG["env_file"], 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('TELEGRAM_BOT_TOKEN='):
                    token = line.split('=', 1)[1].strip().strip('"').strip("'")
                elif line.startswith('CARGATE_CHAT_ID='):
                    chat_id = line.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return token, chat_id

def send_telegram_alert(message):
    """Send alert via Telegram bot API."""
    token, chat_id = get_telegram_credentials()
    if not token:
        print("[Alert] No Telegram bot token found")
        return False
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(token)
        data = urllib.parse.urlencode({
            'chat_id': chat_id,
            'text': message,
        }).encode('utf-8')
        req = urllib.request.Request(url, data=data)
        resp = urllib.request.urlopen(req, timeout=5)
        if resp.status == 200:
            print("[Alert] Telegram alert sent")
            return True
        else:
            print("[Alert] Telegram returned status {}".format(resp.status))
            return False
    except Exception as e:
        print("[Alert] Error sending Telegram alert: {}".format(e))
        return False

class CarAlarmDaemon:
    def __init__(self):
        self.bus = dbus.SystemBus()
        self.sensor_service = 'com.nokia.SensorService'
        self.sensor_path = '/SensorManager'
        
        self.manager_obj = self.bus.get_object(self.sensor_service, self.sensor_path)
        self.manager = dbus.Interface(self.manager_obj, 'local.SensorManager')
        
        self._sensor_objects = {}
        self._sensor_props = {}
        self._session_ids = {}
        self._pid = os.getpid()
        
        # Alarm state
        self._armed = False
        self._baseline = {}
        self._alert_active = False
        
        # Alert cooldown: allow re-triggering after this many seconds
        self._last_alert_time = 0
        self._alert_cooldown = 180  # seconds between repeated alerts

        # Auto-rebaseline on sustained magnetometer offset (location shift detection)
        self._sustained_offset_start = None
        self._sustained_offset_sign = 0
        self._sustained_offset_rebaseline_s = 120  # re-baseline after 120s of sustained one-direction offset
        
        # Sensitivity thresholds
        self._mag_threshold = 50.0
        self._mag_absolute_min = 800.0
        self._light_threshold = 5000.0
        self._orientation_threshold = 15
        
        # Proximity debounce
        self._proximity_enabled = False  # disabled: binary near-field sensor (ear proximity), useless for car alarm, causes false triggers
        self._proximity_debounce_s = 2.0
        self._proximity_last_change_time = 0
        self._proximity_last_state = None
        
        self._initialize_sensors()
    
    def _initialize_sensors(self):
        """Get sensor objects from the manager."""
        try:
            sensor_paths = [
                ('magnetometer', '/SensorManager/magnetometersensor', 'local.MagnetometerSensor'),
                ('light', '/SensorManager/alssensor', 'local.ALSSensor'),
                ('orientation', '/SensorManager/orientationsensor', 'local.OrientationSensor'),
                ('proximity', '/SensorManager/proximitysensor', 'local.ProximitySensor')
            ]
            
            for sensor_type, obj_path, interface in sensor_paths:
                try:
                    obj = self.bus.get_object(self.sensor_service, obj_path)
                    self._sensor_objects[sensor_type] = (obj, interface)
                except Exception as e:
                    print(f"Could not get {sensor_type} sensor: {e}")
        except Exception as e:
            print(f"Error initializing sensors: {e}")
    
    def _activate_sensors(self):
        """Request and start sensors to get live data."""
        try:
            for sensor_type in self._sensor_objects.keys():
                try:
                    session_id = self.manager.requestSensor(sensor_type, self._pid)
                    self._session_ids[sensor_type] = session_id
                    
                    sensor_obj, interface = self._sensor_objects[sensor_type]
                    sensor_iface = dbus.Interface(sensor_obj, interface)
                    sensor_iface.start(session_id)
                    
                    try:
                        sensor_iface.setInterval(session_id, 3000)
                    except:
                        pass
                except Exception as e:
                    print(f"Failed to activate {sensor_type} sensor: {e}")
                    self._session_ids[sensor_type] = None
        except Exception as e:
            print(f"Error activating sensors: {e}")
    
    def _deactivate_sensors(self):
        """Stop all sensors and release sessions."""
        try:
            for sensor_type, session_id in self._session_ids.items():
                if session_id is not None:
                    try:
                        if sensor_type in self._sensor_objects:
                            obj, interface = self._sensor_objects[sensor_type]
                            sensor_iface = dbus.Interface(obj, interface)
                            sensor_iface.stop(session_id)
                        self.manager.releaseSensor(sensor_type, session_id)
                    except Exception as e:
                        print(f"Error cleaning up {sensor_type} sensor: {e}")
        except Exception as e:
            print(f"Error during sensor cleanup: {e}")
    
    def _get_sensor_properties(self, sensor_type: str) -> Optional[dbus.Interface]:
        if sensor_type not in self._sensor_objects:
            return None
        
        if sensor_type in self._sensor_props:
            return self._sensor_props[sensor_type]
        
        obj, _ = self._sensor_objects[sensor_type]
        try:
            props = dbus.Interface(obj, 'org.freedesktop.DBus.Properties')
            self._sensor_props[sensor_type] = props
            return props
        except Exception as e:
            print(f"Error getting properties for {sensor_type}: {e}")
            return None
    
    def _read_magnetometer(self) -> Optional[Tuple[float, float, float, float, float]]:
        props = self._get_sensor_properties('magnetometer')
        if not props:
            return None
        try:
            magnetic_field = props.Get('local.MagnetometerSensor', 'magneticField')
            if len(magnetic_field) >= 4:
                timestamp = float(magnetic_field[0]) / 1000000.0
                x = float(magnetic_field[1])
                y = float(magnetic_field[2])
                z = float(magnetic_field[3])
                magnitude = math.sqrt(x*x + y*y + z*z)
                return (timestamp, x, y, z, magnitude)
            else:
                return None
        except Exception as e:
            return None
    
    def _read_light(self) -> Optional[float]:
        props = self._get_sensor_properties('light')
        if not props:
            return None
        try:
            lux_data = props.Get('local.ALSSensor', 'lux')
            if len(lux_data) >= 2:
                illuminance = float(lux_data[1])
                return illuminance
            else:
                return None
        except Exception as e:
            return None
    
    def _read_orientation(self) -> Optional[Tuple[float, int]]:
        props = self._get_sensor_properties('orientation')
        if not props:
            return None
        for prop_name in ['orientation', 'state']:
            try:
                value = props.Get('local.OrientationSensor', prop_name)
                if isinstance(value, tuple) and len(value) >= 2:
                    timestamp = float(value[0]) / 1000000.0
                    state = int(value[1])
                    return (timestamp, state)
                else:
                    return (time.time(), int(value))
            except:
                continue
        return None
    
    def _read_proximity(self) -> Optional[Tuple[float, int]]:
        props = self._get_sensor_properties('proximity')
        if not props:
            return None
        for prop_name in ['proximity', 'state']:
            try:
                value = props.Get('local.ProximitySensor', prop_name)
                if isinstance(value, tuple) and len(value) >= 2:
                    timestamp = float(value[0]) / 1000000.0
                    state = int(value[1])
                    return (timestamp, state)
                else:
                    return (time.time(), int(value))
            except:
                continue
        return None
    
    def _read_all_sensors(self) -> Dict[str, Optional[Any]]:
        return {
            'magnetometer': self._read_magnetometer(),
            'light': self._read_light(),
            'orientation': self._read_orientation(),
            'proximity': self._read_proximity(),
            'timestamp': time.time()
        }
    
    def _is_significant_change(self, current: Dict, baseline: Dict) -> Tuple[bool, List[str]]:
        changes = []
        
        if current['magnetometer'] and baseline['magnetometer']:
            curr_mag = current['magnetometer'][4]
            base_mag = baseline['magnetometer'][4]
            mag_change = abs(curr_mag - base_mag)
            mag_pct = (mag_change / base_mag * 100) if base_mag > 0 else 0
            if mag_change > self._mag_absolute_min and mag_pct > self._mag_threshold:
                changes.append(f"Magnetometer: {mag_change:.0f} units ({mag_pct:.1f}%, thr {self._mag_threshold}%)")
        
        if current['light'] is not None and baseline['light'] is not None:
            light_change = abs(current['light'] - baseline['light'])
            if light_change > self._light_threshold:
                changes.append(f"Light: {light_change:.1f} lux (threshold: {self._light_threshold} lux)")
        
        if current['orientation'] and baseline['orientation']:
            curr_state = current['orientation'][1]
            base_state = baseline['orientation'][1]
            if curr_state != base_state:
                changes.append(f"Orientation: {base_state} -> {curr_state}")
        
        if self._proximity_enabled and current['proximity'] and baseline['proximity']:
            curr_prox = current['proximity'][1]
            base_prox = baseline['proximity'][1]
            now = time.time()
            if curr_prox != base_prox:
                if curr_prox != self._proximity_last_state:
                    self._proximity_last_state = curr_prox
                    self._proximity_last_change_time = now
                elif (now - self._proximity_last_change_time) >= self._proximity_debounce_s:
                    changes.append(f"Proximity: {base_prox} -> {curr_prox}")
            else:
                self._proximity_last_state = None
                self._proximity_last_change_time = 0
        
        return len(changes) > 0, changes
    
    def arm(self):
        print("Arming car alarm...")
        print("Taking baseline sensor readings...")
        
        baseline_samples = []
        for i in range(5):
            readings = self._read_all_sensors()
            valid_readings = {k: v for k, v in readings.items() if v is not None and k != 'timestamp'}
            if len(valid_readings) >= 3:
                baseline_samples.append(readings)
            time.sleep(0.2)
        
        if not baseline_samples:
            print("ERROR: Could not get sufficient sensor readings for baseline")
            return False
        
        self._baseline = baseline_samples[-1]
        self._armed = True
        self._alert_active = False
        
        # Write PID file
        try:
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(PID_FILE, 'w') as f:
                f.write(str(os.getpid()))
        except Exception as e:
            print(f"Warning: could not write PID file: {e}")
        
        print("Alarm ARMED!")
        print(f"Baseline readings:")
        print(f"  Magnetometer: {self._baseline['magnetometer']}")
        print(f"  Light: {self._baseline['light']} lux")
        print(f"  Orientation: {self._baseline['orientation']}")
        print(f"  Proximity: {self._baseline['proximity']}")
        print(f"  Sensitivity: Mag>{self._mag_threshold}%/{self._mag_absolute_min:.0f}units, Light>{self._light_threshold}lux")
        print("-" * 50)
        return True
    
    def disarm(self):
        if self._armed:
            print("\nDisarming car alarm...")
            self._armed = False
            self._alert_active = False
            self._baseline = {}
            print("Alarm DISARMED")
        else:
            print("Alarm is already disarmed.")
    
    def _cleanup_pid(self):
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
        except:
            pass
    
    def _get_location_context(self):
        try:
            state_path = Path(CONFIG["alarm_dir"]) / "db" / "location_state.json"
            if not state_path.exists():
                return None
            with open(state_path) as f:
                state = json.load(f)
            ts = state.get("ts", 0)
            if time.time() - ts > 120:
                return None
            wifi_aps = state.get("wifi", [])[:3]
            wifi_str = ", ".join([f"{a['bssid'][:8]}({a['signal']})" for a in wifi_aps])
            cell = state.get("cell", {})
            cell_str = f"{cell.get('Name','-')} cid={cell.get('CellId','-')} {cell.get('Technology','')}" if cell else "-"
            ipgeo = state.get("ipgeo", {})
            geo_str = f"{ipgeo.get('lat','?')},{ipgeo.get('lon','?')} {ipgeo.get('city','')}" if (ipgeo and ipgeo.get("status") == "success") else "-"
            return f"📍 WiFi: {wifi_str}\n📶 Cell: {cell_str}\n🌐 Geo: {geo_str}"
        except Exception:
            pass
        return None

    def _check_sustained_offset(self, current):
        """Detect sustained magnetometer offset and silently re-baseline.

        A location shift causes a one-time step in magnetometer magnitude that
        persists in the same direction. A real tamper event oscillates as the
        intruder moves. If the offset is sustained in one direction for >N
        seconds, re-baseline to the new magnetic environment.
        """
        if not current['magnetometer'] or not self._baseline.get('magnetometer'):
            return

        curr_mag = current['magnetometer'][4]
        base_mag = self._baseline['magnetometer'][4]
        mag_change = abs(curr_mag - base_mag)
        mag_pct = (mag_change / base_mag * 100) if base_mag > 0 else 0
        curr_sign = 1 if curr_mag > base_mag else -1

        if mag_pct > self._mag_threshold:
            if self._sustained_offset_start is None:
                # Start tracking a potential sustained offset
                self._sustained_offset_start = time.time()
                self._sustained_offset_sign = curr_sign
            elif curr_sign != self._sustained_offset_sign:
                # Direction flipped = oscillation, not a location shift. Reset.
                self._sustained_offset_start = time.time()
                self._sustained_offset_sign = curr_sign

            if self._sustained_offset_start is not None:
                sustained = time.time() - self._sustained_offset_start
                if sustained >= self._sustained_offset_rebaseline_s:
                    old_mag = base_mag
                    new_baseline = self._read_all_sensors()
                    if new_baseline.get('magnetometer'):
                        self._baseline = new_baseline
                        new_mag = self._baseline['magnetometer'][4]
                        print('[Auto-rebaseline] Sustained %.1f%% offset for %.0fs, '
                              're-baselining: %.0fT -> %.0fT' % (mag_pct, sustained, old_mag, new_mag))
                    self._sustained_offset_start = None
                    self._sustained_offset_sign = 0
        else:
            # Offset gone - reset tracking
            self._sustained_offset_start = None
            self._sustained_offset_sign = 0

    def trigger_alert(self, changes: List[str]):
        now = time.time()
        
        if self._alert_active:
            elapsed = now - self._last_alert_time
            if elapsed < self._alert_cooldown:
                return
            self._alert_active = False
        
        self._alert_active = True
        self._last_alert_time = now
        
        # self._baseline = self._read_all_sensors()  # disabled: keep original baseline
        
        location_ctx = self._get_location_context()
        
        alert_msg = "🚨 *{} ALARM TRIGGERED!* 🚨\n\n".format(
            CONFIG["device_label"].upper())
        alert_msg += "Changes detected:\n"
        for change in changes:
            alert_msg += "• {}\n".format(change)
        if location_ctx:
            alert_msg += location_ctx + "\n"
        alert_msg += "\nTime: {}".format(time.strftime('%Y-%m-%d %H:%M:%S'))
        
        send_telegram_alert(alert_msg)

        # Evidence capture on the peripheral (detached — never blocks the
        # sensor loop). Optional: without a capture hook the alarm still fires.
        hook = CONFIG["capture_script"] or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "capture_hook.py")
        if not os.path.exists(hook):
            print(f"No capture hook at {hook} — skipping evidence capture")
        else:
            try:
                subprocess.Popen(
                    ["python3", hook,
                     "--reason", " ".join(changes)[:80]],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                print("Peripheral capture launched (detached)")
            except Exception as e:
                print(f"Peripheral capture launch failed: {e}")
        
        print("\n" + "!"*60)
        print("*** CAR ALARM TRIGGERED ***")
        print("!"*60)
        for change in changes:
            print(f"  - {change}")
        if location_ctx:
            print(f"  {location_ctx}")
        print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Cooldown: {self._alert_cooldown}s before next alert")
        print("!"*60 + "\n")
    
    def run(self):
        print("Car Alarm Daemon Starting...")
        
        if not self.arm():
            print("Failed to arm alarm. Exiting.")
            return
        
        self._activate_sensors()
        
        def signal_handler(sig, frame):
            print('\nShutting down...')
            self.disarm()
            self._deactivate_sensors()
            self._cleanup_pid()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            while True:
                current = self._read_all_sensors()
                changed, changes = self._is_significant_change(current, self._baseline)
                
                if changed:
                    self.trigger_alert(changes)

                # Auto-rebaseline on sustained magnetometer offset (e.g. car moved to new location)
                self._check_sustained_offset(current)

                if int(time.time()) % 10 == 0:
                    mag_val = current['magnetometer'][4] if current['magnetometer'] else 0
                    light_val = current['light'] if current['light'] else 0
                    orient_state = current['orientation'][1] if current['orientation'] else -1
                    prox_state = current['proximity'][1] if current['proximity'] else -1
                    print(f"[{time.strftime('%H:%M:%S')}] Mag:{mag_val:.1f}T Light:{light_val:.0f}lx Orient:{orient_state} Prox:{prox_state}")
                
                time.sleep(3)
                
        except KeyboardInterrupt:
            print("\nShutting down...")
        except Exception as e:
            print(f"Error in monitor loop: {e}")
        finally:
            self.disarm()
            self._deactivate_sensors()
            self._cleanup_pid()



def daemonize():
    import os
    log_dir = os.path.join(CONFIG["alarm_dir"], "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "cargate.log")
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write("fork #1 failed: " + str(e) + "\n")
        sys.exit(1)
    os.chdir("/")
    os.setsid()
    os.umask(0)
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write("fork #2 failed: " + str(e) + "\n")
        sys.exit(1)
    # Redirect stdin from /dev/null, stdout/stderr to log file
    sys.stdout.flush()
    sys.stderr.flush()
    with open("/dev/null", 'r') as fnull:
        os.dup2(fnull.fileno(), sys.stdin.fileno())
    with open(log_path, 'a+') as flog:
        os.dup2(flog.fileno(), sys.stdout.fileno())
        os.dup2(flog.fileno(), sys.stderr.fileno())


def do_disarm():
    if not PID_FILE.exists():
        print("Alarm is not running (no PID file).")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        print("PID file is corrupted.")
        PID_FILE.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to alarm daemon (PID {pid}).")
        # Wait briefly, then verify
        time.sleep(1)
        try:
            os.kill(pid, 0)
            print("Daemon still running — may need manual kill.")
        except ProcessLookupError:
            print("Alarm daemon stopped.")
            PID_FILE.unlink(missing_ok=True)
    except ProcessLookupError:
        print("Daemon was not running (stale PID file).")
        PID_FILE.unlink(missing_ok=True)
    except PermissionError:
        print(f"Permission denied to signal PID {pid}. Run as same user.")


def do_status():
    if not PID_FILE.exists():
        print("Alarm: DISARMED (not running)")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        print("Alarm: UNKNOWN (corrupt PID file)")
        return
    try:
        os.kill(pid, 0)
        print(f"Alarm: ARMED and RUNNING (PID {pid})")
    except ProcessLookupError:
        print("Alarm: DISARMED (stale PID file)")


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(line_buffering=True)  # truthful journal timestamps
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="cargate — car alarm daemon for Ubuntu Touch")
    parser.add_argument('-a', '--arm', action='store_true', help='Start and arm the alarm daemon')
    parser.add_argument('-d', '--disarm', action='store_true', help='Stop the alarm daemon')
    parser.add_argument('-b', '--daemon', action='store_true', help='Run as background daemon')
    parser.add_argument('--status', action='store_true', help='Check if daemon is running')
    args = parser.parse_args()
    
    if args.disarm:
        do_disarm()
    elif args.status:
        do_status()
    else:
        if args.daemon:
            daemonize()
        alarm = CarAlarmDaemon()
        alarm.run()
