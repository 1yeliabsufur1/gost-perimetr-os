#!/usr/bin/env python3
"""GOST/PERIMETR OS backend: telemetry, TV/guide broadcast engine, kiosk control.

Single-file asyncio service. Serves a WebSocket control/telemetry channel on
127.0.0.1:8765 and a Range-capable static HTTP server on 127.0.0.1:8766.
All optional hardware (OBD2, GPIO pad, MCP3008 pots, NMEA GPS, dashcam) must
degrade gracefully when absent -- never let missing hardware crash the loop.
"""
import asyncio
import json
import os
import re
import signal
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import websockets
except ImportError:
    websockets = None

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.environ.get("GOST_APP", str(BASE_DIR / "app")))
MEDIA_DIR = Path(os.environ.get("GOST_MEDIA", str(BASE_DIR / "media")))
MAPS_DIR = Path(os.environ.get("GOST_MAPS", str(BASE_DIR / "maps")))
STATE_DIR = Path(os.environ.get("GOST_STATE", str(BASE_DIR / "state")))
for _d in (STATE_DIR, MAPS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

WS_HOST, WS_PORT = "127.0.0.1", 8765
HTTP_HOST, HTTP_PORT = "127.0.0.1", 8766
MPV_SOCK = str(STATE_DIR / "mpv.sock") if os.name != "nt" else "/tmp/gost-mpv.sock"

WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts"}
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".opus"}
YOUTUBE_CHANNEL = "83"


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def _num(v):
    """Unwrap a python-obd pint Quantity (or plain number) to a float."""
    try:
        return float(v.magnitude)
    except AttributeError:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------- config ----

DEFAULT_CONFIG = {
    "setup_done": False,
    "source_mode": "AUTO",
    "theme": "gost",
    "guide3": {},
    "dashcam": False,
}


class ConfigStore:
    def __init__(self, path):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except Exception as e:
                log("config load failed:", e)

    def save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)
        except Exception as e:
            log("config save failed:", e)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()


def sanitize_guide3(guide3):
    """Drop overlapping blocks (earliest start wins). Belt-and-suspenders --
    the frontend picker also refuses overlaps, but the backend never trusts it."""
    clean = {}
    if not isinstance(guide3, dict):
        return clean
    for chan, days in guide3.items():
        clean[chan] = {}
        if not isinstance(days, dict):
            continue
        for day in WEEKDAYS:
            blocks = days.get(day, [])
            if not isinstance(blocks, list):
                blocks = []
            blocks = sorted(
                (b for b in blocks if isinstance(b, dict)),
                key=lambda b: b.get("s", 0),
            )
            kept = []
            cursor = -1
            for b in blocks:
                s, d, f = b.get("s"), b.get("d"), b.get("f")
                if not isinstance(s, (int, float)) or not isinstance(d, (int, float)):
                    continue
                if d <= 0 or s < 0 or s + d > 24 * 60:
                    continue
                if s < cursor:
                    continue
                kept.append({"s": int(s), "d": int(d), "f": f})
                cursor = s + d
            clean[chan][day] = kept
    return clean


# ------------------------------------------------------------ durations -----

def probe_duration(path: Path):
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return round(float(out.stdout.strip()))
    except Exception as e:
        log("ffprobe failed for", path, ":", e)
        return None


class DurationCache:
    """Cache keyed by path+mtime so replacing a file invalidates its entry."""

    def __init__(self, path: Path):
        self.path = path
        self.map = {}
        if path.exists():
            try:
                self.map = json.loads(path.read_text())
            except Exception:
                self.map = {}

    def _key(self, p: Path):
        try:
            st = p.stat()
        except OSError:
            return None
        return f"{p}|{st.st_mtime_ns}"

    def get(self, p: Path):
        key = self._key(p)
        if key is None:
            return None
        if key in self.map:
            return self.map[key]
        dur = probe_duration(p)
        if dur is not None:
            self.map[key] = dur
            self._save()
        return dur

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.map))
        except Exception as e:
            log("duration cache save failed:", e)


# --------------------------------------------------------- media library ----

def extra_media_roots():
    """USB sticks mounted with a TV/ or MUSIC/ folder at their top level."""
    roots = []
    for base in (Path("/media"), Path("/mnt")):
        if not base.is_dir():
            continue
        try:
            for user_dir in base.iterdir():
                if not user_dir.is_dir():
                    continue
                try:
                    for stick in user_dir.iterdir():
                        if stick.is_dir() and ((stick / "TV").is_dir() or (stick / "MUSIC").is_dir()):
                            roots.append(stick)
                except OSError:
                    continue
        except OSError:
            continue
    return roots


def scan_channels():
    channels = {}
    roots = [MEDIA_DIR / "TV"] + [r / "TV" for r in extra_media_roots()]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.strip().upper() == "COMMERCIALS":
                continue
            m = re.match(r"^(\d{1,2})\s+(.+)$", entry.name.strip())
            if not m:
                continue
            num = str(int(m.group(1)))
            if not (3 <= int(num) <= 82):
                continue
            try:
                files = sorted(f for f in entry.iterdir() if f.suffix.lower() in VIDEO_EXT)
            except OSError:
                files = []
            ch = channels.setdefault(num, {"name": m.group(2), "path": entry, "files": []})
            ch["files"].extend(files)
    return channels


def scan_commercials():
    spots = []
    roots = [MEDIA_DIR / "TV" / "COMMERCIALS"] + [r / "TV" / "COMMERCIALS" for r in extra_media_roots()]
    for root in roots:
        if root.is_dir():
            try:
                spots.extend(sorted(f for f in root.iterdir() if f.suffix.lower() in VIDEO_EXT))
            except OSError:
                pass
    return spots


def scan_audio_pool(pool_name):
    """pool_name: 'MUSIC' or 'PODCASTS'. Genre convention: the first path
    segment under the pool folder (media/MUSIC/<GENRE>/...); files dropped
    directly in the pool root are 'UNSORTED'."""
    tracks = []
    roots = [MEDIA_DIR / pool_name] + [r / pool_name for r in extra_media_roots()]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for f in sorted(root.rglob("*")):
                if f.suffix.lower() not in AUDIO_EXT:
                    continue
                try:
                    rel_parts = f.relative_to(root).parts
                except ValueError:
                    continue
                genre = rel_parts[0] if len(rel_parts) > 1 else "UNSORTED"
                tracks.append({"path": f, "genre": genre})
        except OSError:
            pass
    return tracks


def scan_music():
    return scan_audio_pool("MUSIC")


def scan_podcasts():
    return scan_audio_pool("PODCASTS")


# ------------------------------------------------------- channel resolver ---

def pick_commercial(commercials, now):
    """Changes every 30s (matches the re-resolve cadence) rather than per-call."""
    if not commercials:
        return None
    idx = int(now // 30) % len(commercials)
    return commercials[idx]


def resolve_channel(chan, channels, commercials, guide3, dur_cache, now=None):
    """Pure function: given the library + schedule, decide what should be on
    screen for `chan` right now. Never assumes a fixed clip length."""
    now = now if now is not None else time.time()
    dt = datetime.fromtimestamp(now)
    weekday = WEEKDAYS[(dt.weekday() + 1) % 7]  # python Mon=0 -> want sun=0
    minute_of_day = dt.hour * 60 + dt.minute
    sec_of_minute = dt.second

    ch = channels.get(chan)
    days = guide3.get(chan) or {}
    schedule = days.get(weekday, []) if isinstance(days, dict) else []
    # A channel with ANY programming (on any day) is a curated station: days
    # without blocks are gaps -> commercials/static, NEVER the 24/7 rotation.
    # The rotation is only for dump-folder channels never touched in GUIDE.
    has_any_programming = isinstance(days, dict) and any(
        days.get(d) for d in WEEKDAYS
    )

    if schedule or has_any_programming:
        for b in schedule:
            s, d, f = b["s"], b["d"], b["f"]
            if s <= minute_of_day < s + d:
                offset = (minute_of_day - s) * 60 + sec_of_minute
                fpath = (ch["path"] / f) if ch and f else None
                if fpath and fpath.exists():
                    return {"kind": "show", "file": str(fpath), "offset": offset,
                            "remaining": d * 60 - offset}
                return {"kind": "static", "reason": "missing_file"}
        upcoming = [b["s"] for b in schedule if b["s"] > minute_of_day]
        gap_end = min(upcoming) if upcoming else 24 * 60
        gap_secs = max(1, (gap_end - minute_of_day) * 60 - sec_of_minute)
        spot = pick_commercial(commercials, now)
        if spot is not None:
            spot_dur = dur_cache.get(spot) or 30
            length = min(spot_dur, gap_secs)
            return {"kind": "commercial", "file": str(spot), "offset": 0, "length": length}
        # `until` = seconds to the next scheduled block (or midnight) so the
        # player can re-resolve exactly on the boundary.
        return {"kind": "static", "reason": "gap_no_commercials", "until": gap_secs}

    # never-programmed channel: continuous pseudo-broadcast, seeded by epoch
    if ch and ch["files"]:
        files = ch["files"]
        durations = [dur_cache.get(f) or 300 for f in files]
        total = sum(durations)
        if total <= 0:
            return {"kind": "static", "reason": "no_duration"}
        seed = sum(ord(c) for c in chan) * 997
        pos = int((now + seed) % total)
        acc = 0
        for f, d in zip(files, durations):
            if pos < acc + d:
                return {"kind": "show", "file": str(f), "offset": pos - acc,
                         "remaining": d - (pos - acc)}
            acc += d
        return {"kind": "static", "reason": "rotation_fallthrough"}

    return {"kind": "static", "reason": "off_air"}


# ---------------------------------------------------------------- player ----

class TVPlayer:
    def __init__(self, state):
        self.state = state
        self.proc = None
        self.current_chan = None
        self.mode = "off"  # off / playing / static / app
        self.paused = False
        self.last_resolution = None
        self.boundary_handle = None

    def _cancel_boundary(self):
        if self.boundary_handle is not None:
            self.boundary_handle.cancel()
            self.boundary_handle = None

    def _schedule_boundary(self, res):
        """Minute-accurate handoff: re-resolve exactly when this show/
        commercial/gap ends, so the next block starts on its scheduled
        minute instead of up to 30s late on the coarse supervisor poll
        (which stays as a safety net)."""
        delay = None
        if res["kind"] == "show":
            delay = res.get("remaining")
        elif res["kind"] == "commercial":
            delay = res.get("length")
        else:
            delay = res.get("until")
        if delay is None or delay <= 0 or delay > 24 * 3600:
            return
        loop = asyncio.get_event_loop()

        def _fire():
            self.boundary_handle = None
            asyncio.ensure_future(self._resolve_and_play())

        self.boundary_handle = loop.call_later(delay + 0.5, _fire)

    async def tune(self, chan):
        self.current_chan = chan
        if chan == YOUTUBE_CHANNEL:
            self._cancel_boundary()
            await self.stop_proc()
            self.mode = "app"
            await self.state.launch_app("https://www.youtube.com/tv")
            return
        await self._resolve_and_play()

    async def _resolve_and_play(self):
        self._cancel_boundary()
        await self.stop_proc()
        if self.current_chan is None:
            self.mode = "off"
            return
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(
            None, resolve_channel, self.current_chan, self.state.channels,
            self.state.commercials, self.state.config.get("guide3", {}), self.state.dur_cache,
        )
        self.last_resolution = res
        self._schedule_boundary(res)
        if res["kind"] == "static":
            self.mode = "static"
            return
        args = ["mpv", "--fs", "--really-quiet", "--osc=no",
                f"--input-ipc-server={MPV_SOCK}", f"--start=+{res['offset']}"]
        if res["kind"] == "commercial":
            args.append(f"--length={res['length']}")
        args.append(res["file"])
        try:
            self.proc = await asyncio.create_subprocess_exec(*args)
            self.mode = "playing"
            self.paused = False
        except FileNotFoundError:
            log("mpv binary not found, entering static mode")
            self.proc = None
            self.mode = "static"

    async def stop_proc(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None

    async def _send_mpv(self, cmd):
        try:
            reader, writer = await asyncio.open_unix_connection(MPV_SOCK)
            writer.write((json.dumps({"command": cmd}) + "\n").encode())
            await writer.drain()
            writer.close()
        except Exception as e:
            log("mpv ipc failed:", e)

    async def pause_toggle(self):
        if self.mode != "playing":
            return
        self.paused = not self.paused
        await self._send_mpv(["set_property", "pause", self.paused])

    async def step_channel(self, delta):
        chans = sorted(self.state.channels.keys(), key=int)
        if not chans:
            return
        if self.current_chan not in chans:
            self.current_chan = chans[0]
        else:
            i = chans.index(self.current_chan)
            self.current_chan = chans[(i + delta) % len(chans)]
        await self._resolve_and_play()

    async def exit(self):
        self._cancel_boundary()
        await self.stop_proc()
        self.current_chan = None
        self.mode = "off"

    async def supervisor_tick(self):
        """Re-resolve when mpv exits (commercial ended / file ended / crashed)."""
        if self.mode == "playing" and self.proc and self.proc.returncode is not None:
            await self._resolve_and_play()

    async def periodic_reresolve(self):
        """Called every ~30s so a gap-to-show transition is picked up even
        while mpv is still happily playing a long commercial-length static."""
        if self.current_chan is not None and self.mode != "app":
            await self._resolve_and_play()

    def status(self):
        return {
            "type": "tv.status",
            "chan": self.current_chan,
            "mode": self.mode,
            "paused": self.paused,
            "resolution": {k: v for k, v in (self.last_resolution or {}).items() if k != "file"},
        }


# ------------------------------------------------------------- telemetry ----

FAST_PIDS = ["SPEED", "RPM", "INTAKE_PRESSURE", "MAF"]
SLOW_PIDS = ["FUEL_LEVEL", "COOLANT_TEMP", "CONTROL_MODULE_VOLTAGE", "OIL_TEMP",
             "BAROMETRIC_PRESSURE", "HYBRID_BATTERY_REMAINING"]


class Telemetry:
    def __init__(self, state):
        self.state = state
        self.conn = None
        self.vtype = None
        self.values = {}
        self.derived = {}
        self.connected = False
        self.demo_t0 = time.time()
        self.dtc_path = STATE_DIR / "dtcs.json"
        self.dtcs = {}
        if self.dtc_path.exists():
            try:
                self.dtcs = json.loads(self.dtc_path.read_text())
            except Exception:
                self.dtcs = {}

    async def connect_obd(self):
        try:
            import obd
        except ImportError:
            return False
        try:
            import glob
            ports = glob.glob("/dev/ttyUSB*")
            self.conn = await asyncio.get_event_loop().run_in_executor(
                None, lambda: obd.OBD(portstr=ports[0] if ports else None, fast=False, timeout=2)
            )
            self.connected = bool(self.conn and self.conn.is_connected())
        except Exception as e:
            log("OBD connect failed:", e)
            self.connected = False
        return self.connected

    async def _query(self, pid_name):
        import obd
        cmd = getattr(obd.commands, pid_name, None)
        if cmd is None or self.conn is None:
            return None
        try:
            r = await asyncio.get_event_loop().run_in_executor(None, self.conn.query, cmd)
            if r is None or r.is_null():
                return None
            return r.value
        except Exception:
            return None

    async def poll_fast(self):
        for pid in FAST_PIDS:
            v = await self._query(pid)
            if v is not None:
                self.values[pid] = v

    async def poll_slow(self):
        for pid in SLOW_PIDS:
            v = await self._query(pid)
            if v is not None:
                self.values[pid] = v

    async def poll_dtc(self):
        try:
            import obd
        except ImportError:
            return
        try:
            r = await asyncio.get_event_loop().run_in_executor(None, self.conn.query, obd.commands.GET_DTC)
            if r is None or r.is_null():
                return
            now_iso = datetime.now().isoformat()
            changed = False
            for code, desc in (r.value or []):
                if code not in self.dtcs:
                    self.dtcs[code] = {"first_seen": now_iso, "desc": desc or ""}
                    changed = True
            if changed:
                self.dtc_path.write_text(json.dumps(self.dtcs, indent=2))
        except Exception as e:
            log("DTC poll failed:", e)

    def detect_vtype(self):
        if self.vtype:
            return
        if self.values.get("HYBRID_BATTERY_REMAINING") is not None:
            self.vtype = "hybrid"
        elif self.values.get("FUEL_LEVEL") is not None:
            self.vtype = "gas"
        elif self.values.get("SPEED") is not None:
            self.vtype = "ev"

    def derive(self):
        maf = _num(self.values.get("MAF"))
        speed_kph = _num(self.values.get("SPEED"))
        mph = speed_kph * 0.621371 if speed_kph is not None else None
        mapv = _num(self.values.get("INTAKE_PRESSURE"))
        baro = _num(self.values.get("BAROMETRIC_PRESSURE"))
        boost_psi = max(0.0, (mapv - baro) * 0.145038) if (mapv is not None and baro is not None) else None
        mpg = 11.3 * mph / maf if (maf is not None and maf >= 0.5 and mph is not None) else None
        coolant_c = _num(self.values.get("COOLANT_TEMP"))
        oil_c = _num(self.values.get("OIL_TEMP"))
        self.derived = {
            "mph": mph,
            "boost_psi": boost_psi,
            "mpg": mpg,
            "coolant_f": (coolant_c * 9 / 5 + 32) if coolant_c is not None else None,
            "oil_f": (oil_c * 9 / 5 + 32) if oil_c is not None else None,
        }

    def demo_tick(self):
        t = time.time() - self.demo_t0
        self.vtype = "ev"
        self.connected = True
        speed_mph = 35 + 25 * abs(((t / 20) % 2) - 1)
        soc = max(5.0, 82 - (t / 60) * 0.8)
        batt12 = 12.6 + 0.3 * ((t / 13) % 1)
        if int(t / 45) % 6 == 5:
            batt12 = 11.8  # periodic sag to exercise the HUD alarm
        self.values = {
            "SPEED": speed_mph / 0.621371,
            "RPM": 0,
            "INTAKE_PRESSURE": 101.3,
            "MAF": 0.0,
            "COOLANT_TEMP": 68 + 5 * ((t / 30) % 1),
            "CONTROL_MODULE_VOLTAGE": batt12,
            "BAROMETRIC_PRESSURE": 101.3,
            "HYBRID_BATTERY_REMAINING": soc,
        }
        self.derived = {
            "mph": speed_mph, "boost_psi": 0.0, "mpg": None,
            "coolant_f": self.values["COOLANT_TEMP"] * 9 / 5 + 32, "oil_f": None,
        }
        if "P1A42" not in self.dtcs:
            self.dtcs["P1A42"] = {"first_seen": datetime.now().isoformat(),
                                   "desc": "Hybrid battery cell imbalance (demo)"}

    async def poll_loop(self):
        last_slow = 0.0
        last_dtc = 0.0
        while True:
            try:
                mode = self.state.config.get("source_mode", "AUTO")
                if mode == "DEMO":
                    self.demo_tick()
                    await asyncio.sleep(0.2)
                    continue
                if not self.connected:
                    ok = await self.connect_obd()
                    if not ok:
                        self.values = {}
                        await asyncio.sleep(3)
                        continue
                await self.poll_fast()
                now = time.time()
                if now - last_slow > 2:
                    await self.poll_slow()
                    last_slow = now
                if now - last_dtc > 30:
                    await self.poll_dtc()
                    last_dtc = now
                self.detect_vtype()
                self.derive()
            except Exception as e:
                log("telemetry loop error (continuing):", e)
                self.connected = False
            await asyncio.sleep(0.2)

    def snapshot(self):
        return {
            "type": "telemetry",
            "connected": self.connected,
            "source_mode": self.state.config.get("source_mode", "AUTO"),
            "vtype": self.vtype or "unknown",
            "values": self.values,
            "derived": self.derived,
            "dtcs": self.dtcs,
        }


# ------------------------------------------------------------ GPIO input ----

DEFAULT_GPIO_PINS = {"up": 5, "down": 6, "left": 13, "right": 19, "enter": 26}


class InputManager:
    """5-way pad, active-low: each button shorts its GPIO to GND (internal
    pull-ups enabled). Pins are overridable via config.json "gpio_pins" so a
    custom PCB with a different fanout only needs a config edit, not a code
    change. NEVER allocate the same GPIO pin twice -- the left button's
    hold-to-back feature reuses the existing Button object via .hold_time,
    it does not get a second Button() on the same pin (that throws
    GPIOPinInUse and crash-loops the whole backend)."""

    def __init__(self, broadcast_cb, config=None):
        self.broadcast_cb = broadcast_cb
        self.buttons = {}
        pins = dict(DEFAULT_GPIO_PINS)
        if config is not None:
            try:
                overrides = config.get("gpio_pins", {}) or {}
                for k, v in overrides.items():
                    if k in pins and isinstance(v, int):
                        pins[k] = v
            except Exception as e:
                log("gpio_pins config invalid, using defaults:", e)
        self._setup(pins)

    def _setup(self, pins):
        try:
            from gpiozero import Button
        except Exception as e:
            log("gpiozero unavailable, GPIO pad disabled:", e)
            return
        for name, pin in pins.items():
            try:
                btn = Button(pin, pull_up=True, bounce_time=0.05)
            except Exception as e:
                log(f"button '{name}' on GPIO{pin} unavailable:", e)
                continue
            if name == "left":
                # Left is special: a 5s hold is universal-back, a short press
                # is a normal "left". Fire "left" on RELEASE only when the
                # press did NOT complete a hold, so one physical hold doesn't
                # also emit a spurious tab-switch.
                btn.hold_time = 5
                btn.when_held = self._make_cb("back_hold")
                btn.when_released = self._make_left_release(btn)
            else:
                btn.when_pressed = self._make_cb(name)
            self.buttons[name] = btn
        log("GPIO pad ready:", {n: b.pin for n, b in self.buttons.items()})

    def _make_cb(self, name):
        def _cb():
            self.broadcast_cb({"type": "input", "key": name})
        return _cb

    def _make_left_release(self, btn):
        def _cb():
            if not btn.was_held:
                self.broadcast_cb({"type": "input", "key": "left"})
        return _cb


class PotManager:
    """MCP3008 volume/brightness pots over SPI. Optional."""

    def __init__(self, broadcast_cb):
        self.broadcast_cb = broadcast_cb
        self.spi = None
        try:
            import spidev
            self.spi = spidev.SpiDev()
            self.spi.open(0, 0)
            self.spi.max_speed_hz = 1350000
        except Exception as e:
            log("MCP3008/SPI unavailable, pots disabled:", e)
            self.spi = None

    def _read_channel(self, ch):
        if not self.spi:
            return None
        try:
            r = self.spi.xfer2([1, (8 + ch) << 4, 0])
            return ((r[1] & 3) << 8) + r[2]
        except Exception as e:
            log("pot read failed:", e)
            return None

    async def poll_loop(self):
        if not self.spi:
            return
        while True:
            vol = self._read_channel(0)
            bri = self._read_channel(1)
            if vol is not None or bri is not None:
                self.broadcast_cb({"type": "pots", "volume": vol, "brightness": bri})
            await asyncio.sleep(0.3)


def _nmea_to_deg(val, hemi):
    d = float(val)
    deg = int(d / 100)
    minutes = d - deg * 100
    dec = deg + minutes / 60
    return -dec if hemi in ("S", "W") else dec


def parse_nmea(line):
    if not (line.startswith("$GPGGA") or line.startswith("$GNGGA")):
        return None
    parts = line.split(",")
    if len(parts) < 6 or not parts[2] or not parts[4]:
        return None
    try:
        return {"lat": _nmea_to_deg(parts[2], parts[3]), "lon": _nmea_to_deg(parts[4], parts[5])}
    except (ValueError, IndexError):
        return None


class GPSReader:
    def __init__(self, broadcast_cb):
        self.broadcast_cb = broadcast_cb

    async def run(self):
        try:
            import serial
        except ImportError:
            log("pyserial unavailable, GPS disabled")
            return
        import glob
        ports = glob.glob("/dev/ttyACM*") + glob.glob("/dev/serial0")
        if not ports:
            log("no GPS serial port found")
            return
        try:
            ser = serial.Serial(ports[0], 9600, timeout=1)
        except Exception as e:
            log("GPS open failed:", e)
            return
        loop = asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, ser.readline)
                fix = parse_nmea(line.decode(errors="ignore").strip())
                if fix:
                    self.broadcast_cb({"type": "gps", **fix})
            except Exception as e:
                log("GPS read error:", e)
                await asyncio.sleep(1)


class Dashcam:
    def __init__(self):
        self.proc = None

    async def set_enabled(self, on):
        if on and not self.proc:
            out_dir = STATE_DIR / "dashcam"
            out_dir.mkdir(exist_ok=True)
            fname = out_dir / f"{int(time.time())}.h264"
            try:
                self.proc = await asyncio.create_subprocess_exec("rpicam-vid", "-t", "0", "-o", str(fname))
            except FileNotFoundError:
                log("rpicam-vid not available, dashcam disabled")
                self.proc = None
        elif not on and self.proc:
            if self.proc.returncode is None:
                self.proc.terminate()
            self.proc = None


# --------------------------------------------------------------- wifi/pw ----

async def wifi_scan():
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        nets = []
        for line in out.decode(errors="ignore").splitlines():
            parts = line.split(":")
            if parts and parts[0]:
                nets.append({"ssid": parts[0], "signal": parts[1] if len(parts) > 1 else "",
                             "security": parts[2] if len(parts) > 2 else ""})
        return nets
    except Exception as e:
        log("wifi scan failed:", e)
        return []


async def wifi_join(ssid, psk):
    if not ssid:
        return False
    try:
        args = ["nmcli", "dev", "wifi", "connect", ssid]
        if psk:
            args += ["password", psk]
        proc = await asyncio.create_subprocess_exec(*args)
        return await proc.wait() == 0
    except Exception as e:
        log("wifi join failed:", e)
        return False


async def set_password(password):
    if not password or len(password) < 4:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "/usr/local/sbin/gost-setpass", stdin=asyncio.subprocess.PIPE)
        await proc.communicate(input=(password + "\n").encode())
        return proc.returncode == 0
    except Exception as e:
        log("setpass failed:", e)
        return False


async def set_clock(iso_str):
    if not iso_str or "\n" in iso_str or len(iso_str) > 64:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "/usr/local/sbin/gost-settime", stdin=asyncio.subprocess.PIPE)
        await proc.communicate(input=(iso_str + "\n").encode())
        return proc.returncode == 0
    except Exception as e:
        log("settime failed:", e)
        return False


async def run_update_check():
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(BASE_DIR), "fetch", "--dry-run",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return {"ok": proc.returncode == 0, "detail": (out + err).decode(errors="ignore")[:500]}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def system_info():
    info = {"version": "3.0.0", "hostname": socket.gethostname()}
    try:
        with open("/proc/uptime") as f:
            info["uptime_sec"] = float(f.read().split()[0])
    except Exception:
        info["uptime_sec"] = None
    return info


# ------------------------------------------------------------------ state ---

class GostState:
    def __init__(self):
        self.config = ConfigStore(STATE_DIR / "config.json")
        self.dur_cache = DurationCache(STATE_DIR / "durations.json")
        self.channels = {}
        self.commercials = []
        self.music = []
        self.podcasts = []
        self.clients = set()
        self.loop = None
        self.app_proc = None
        self.telemetry = Telemetry(self)
        self.tvplayer = TVPlayer(self)
        self.inputs = InputManager(self.broadcast, self.config)
        self.pots = PotManager(self.broadcast)
        self.gps = GPSReader(self.broadcast)
        self.dashcam = Dashcam()

    def broadcast(self, msg):
        """Thread-safe: gpiozero callbacks fire on their own thread, not the
        event loop thread, so this must never call create_task() directly."""
        if self.loop is None or not self.clients:
            return
        data = json.dumps(msg)
        for ws in list(self.clients):
            fut = asyncio.run_coroutine_threadsafe(ws.send(data), self.loop)
            fut.add_done_callback(lambda f: f.exception() and log("send failed:", f.exception()))

    async def refresh_library(self):
        self.channels = scan_channels()
        self.commercials = scan_commercials()
        self.music = scan_music()
        self.podcasts = scan_podcasts()

    def channel_summary(self):
        return {num: {"name": c["name"], "files": [f.name for f in c["files"]]}
                for num, c in self.channels.items()}

    def _audio_summary(self, tracks):
        out = []
        for t in tracks:
            try:
                rel = str(t["path"].relative_to(MEDIA_DIR))
            except ValueError:
                rel = str(t["path"])
            out.append({"name": t["path"].name, "path": rel, "genre": t["genre"]})
        return out

    def library_payload(self):
        return {
            "type": "library", "channels": self.channel_summary(),
            "music": self._audio_summary(self.music),
            "podcasts": self._audio_summary(self.podcasts),
        }

    async def library_watcher(self):
        prev_sig = None
        while True:
            await self.refresh_library()
            sig = (tuple(sorted(self.channels.keys())), len(self.commercials),
                   len(self.music), len(self.podcasts))
            if sig != prev_sig:
                self.broadcast(self.library_payload())
                prev_sig = sig
            await asyncio.sleep(4)

    async def launch_app(self, url):
        await self.kill_apps()
        for exe in ("chromium-browser", "chromium"):
            try:
                self.app_proc = await asyncio.create_subprocess_exec(exe, "--kiosk", f"--app={url}")
                return
            except FileNotFoundError:
                continue
        log("no chromium binary found, cannot launch app for", url)

    async def kill_apps(self):
        if self.app_proc and self.app_proc.returncode is None:
            self.app_proc.terminate()
            try:
                await asyncio.wait_for(self.app_proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.app_proc.kill()
        self.app_proc = None

    async def universal_back(self):
        await self.kill_apps()
        await self.tvplayer.exit()


# --------------------------------------------------------------- routing ----

async def route_message(state: GostState, ws, msg):
    t = msg.get("type")
    if t == "tv.tune":
        await state.tvplayer.tune(str(msg.get("chan")))
    elif t == "tv.step":
        await state.tvplayer.step_channel(int(msg.get("delta", 1)))
    elif t == "tv.pause":
        await state.tvplayer.pause_toggle()
    elif t == "tv.exit":
        await state.tvplayer.exit()
    elif t == "back":
        await state.universal_back()
    elif t == "config.set":
        key, value = msg.get("key"), msg.get("value")
        if key == "guide3":
            value = sanitize_guide3(value)
        state.config.set(key, value)
        state.broadcast({"type": "config", **state.config.data})
    elif t == "config.get":
        await ws.send(json.dumps({"type": "config", **state.config.data}))
    elif t == "app.launch":
        await state.launch_app(msg.get("url"))
    elif t == "app.kill":
        await state.kill_apps()
    elif t == "wifi.scan":
        await ws.send(json.dumps({"type": "wifi.scan", "networks": await wifi_scan()}))
    elif t == "wifi.join":
        ok = await wifi_join(msg.get("ssid"), msg.get("psk"))
        await ws.send(json.dumps({"type": "wifi.join", "ok": ok}))
    elif t == "dashcam.set":
        await state.dashcam.set_enabled(bool(msg.get("on")))
    elif t == "setpass":
        # Only the password is applied to the system account (via gost-setpass,
        # which always targets the pre-detected GOST_USER from install.sh).
        # The username is stored purely as a display label -- renaming the
        # actual Linux account at runtime is out of scope and risky (UID/home
        # dir/systemd User= all reference the install-time account).
        if msg.get("username"):
            state.config.set("operator_name", msg["username"])
        ok = await set_password(msg.get("password"))
        await ws.send(json.dumps({"type": "setpass", "ok": ok}))
    elif t == "clock.set":
        ok = await set_clock(msg.get("iso", ""))
        await ws.send(json.dumps({"type": "clock.set", "ok": ok}))
    elif t == "update.check":
        await ws.send(json.dumps({"type": "update.check", **await run_update_check()}))
    elif t == "system.info":
        await ws.send(json.dumps({"type": "system.info", **system_info()}))


async def ws_handler(websocket, state: GostState):
    state.clients.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "hello", "vtype": state.telemetry.vtype or "unknown"}))
        await websocket.send(json.dumps(state.library_payload()))
        await websocket.send(json.dumps({"type": "config", **state.config.data}))
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            try:
                await route_message(state, websocket, msg)
            except Exception as e:
                log("route_message error (continuing):", e)
    finally:
        state.clients.discard(websocket)


async def telemetry_broadcast_loop(state: GostState):
    while True:
        state.broadcast(state.telemetry.snapshot())
        await asyncio.sleep(0.2)


async def tv_supervisor_loop(state: GostState):
    tick = 0
    while True:
        await asyncio.sleep(2)
        try:
            await state.tvplayer.supervisor_tick()
            tick += 1
            if tick % 15 == 0:
                await state.tvplayer.periodic_reresolve()
            state.broadcast(state.tvplayer.status())
        except Exception as e:
            log("tv supervisor error (continuing):", e)


# --------------------------------------------------------------- HTTP ------

MIME_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript",
    ".css": "text/css", ".json": "application/json", ".mp4": "video/mp4",
    ".webm": "video/webm", ".mp3": "audio/mpeg", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml",
    ".pmtiles": "application/octet-stream", ".woff2": "font/woff2", ".ico": "image/x-icon",
}


def guess_mime(path: Path):
    return MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")


def resolve_fs_path(url_path: str):
    clean = unquote(urlsplit(url_path).path)
    if clean == "/":
        clean = "/index.html"
    if clean.startswith("/media/"):
        base, rel = MEDIA_DIR, clean[len("/media/"):]
    elif clean.startswith("/maps/"):
        base, rel = MAPS_DIR, clean[len("/maps/"):]
    else:
        base, rel = APP_DIR, clean.lstrip("/")
    target = (base / rel).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


async def _write_status(writer, code, reason, body=b""):
    writer.write(
        f"HTTP/1.1 {code} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()


async def serve_file(writer, url_path, headers, method):
    if urlsplit(url_path).path == "/api/maps":
        files = [p.name for p in MAPS_DIR.glob("*.pmtiles")] if MAPS_DIR.is_dir() else []
        body = json.dumps({"maps": files}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        return

    target = resolve_fs_path(url_path)
    if target is None or not target.is_file():
        await _write_status(writer, 404, "Not Found", b"404 Not Found")
        return

    size = target.stat().st_size
    mime = guess_mime(target)
    start, end = 0, size - 1
    status = "200 OK"
    range_header = headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            rng = range_header[len("bytes="):].split("-")
            if rng[0]:
                start = int(rng[0])
            if len(rng) > 1 and rng[1]:
                end = int(rng[1])
            status = "206 Partial Content"
        except ValueError:
            start, end = 0, size - 1

    length = end - start + 1
    header_lines = [
        f"HTTP/1.1 {status}", f"Content-Type: {mime}", f"Content-Length: {length}",
        "Accept-Ranges: bytes", "Connection: close",
    ]
    if status.startswith("206"):
        header_lines.append(f"Content-Range: bytes {start}-{end}/{size}")
    writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode())
    await writer.drain()
    if method == "HEAD":
        return

    with target.open("rb") as f:
        f.seek(start)
        remaining = length
        chunk = 256 * 1024
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            writer.write(data)
            await writer.drain()
            remaining -= len(data)


async def handle_http_client(reader, writer):
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not request_line:
            return
        try:
            method, path, _ = request_line.decode(errors="ignore").strip().split(" ", 2)
        except ValueError:
            await _write_status(writer, 400, "Bad Request")
            return
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            if b":" in line:
                k, v = line.decode(errors="ignore").split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if method not in ("GET", "HEAD"):
            await _write_status(writer, 405, "Method Not Allowed")
            return
        await serve_file(writer, path, headers, method)
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        log("http error:", e)
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ------------------------------------------------------------------ main ----

async def main():
    state = GostState()
    state.loop = asyncio.get_running_loop()
    await state.refresh_library()

    async def ws_entry(websocket):
        await ws_handler(websocket, state)

    ws_server = await websockets.serve(ws_entry, WS_HOST, WS_PORT)
    http_server = await asyncio.start_server(handle_http_client, HTTP_HOST, HTTP_PORT)
    log(f"WS   ws://{WS_HOST}:{WS_PORT}")
    log(f"HTTP http://{HTTP_HOST}:{HTTP_PORT}  app={APP_DIR} media={MEDIA_DIR} maps={MAPS_DIR}")

    def _term(*_):
        for task in asyncio.all_tasks():
            task.cancel()
    try:
        state.loop.add_signal_handler(signal.SIGTERM, _term)
        state.loop.add_signal_handler(signal.SIGINT, _term)
    except NotImplementedError:
        pass  # not available on Windows dev boxes; py_compile-only there anyway

    tasks = [
        asyncio.create_task(state.telemetry.poll_loop()),
        asyncio.create_task(telemetry_broadcast_loop(state)),
        asyncio.create_task(state.library_watcher()),
        asyncio.create_task(tv_supervisor_loop(state)),
        asyncio.create_task(state.pots.poll_loop()),
        asyncio.create_task(state.gps.run()),
    ]

    async with ws_server, http_server:
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    if websockets is None:
        log("FATAL: 'websockets' package not installed (pip install websockets)")
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
