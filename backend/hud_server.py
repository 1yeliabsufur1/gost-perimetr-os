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
import random
import re
import shutil
import signal
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlencode, quote
import urllib.request

try:
    import websockets
except ImportError:
    websockets = None

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.environ.get("GOST_APP", str(BASE_DIR / "app")))
MEDIA_DIR = Path(os.environ.get("GOST_MEDIA", str(BASE_DIR / "media")))
MAPS_DIR = Path(os.environ.get("GOST_MAPS", str(BASE_DIR / "maps")))
STATE_DIR = Path(os.environ.get("GOST_STATE", str(BASE_DIR / "state")))
ROMS_DIR = Path(os.environ.get("GOST_ROMS", str(BASE_DIR / "roms")))   # user drops game ROMs here
for _d in (STATE_DIR, MAPS_DIR, ROMS_DIR):
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
    "channel_names": {},
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


# -------------------------------------------------- offline map downloads ----
# Turnkey region downloads: bundled `pmtiles` CLI extracts just a region from
# Protomaps' daily planet build via HTTP range requests (only the region is
# fetched, never the whole planet). Presets cover the whole USA + regions;
# a custom bbox is also accepted. (maxzoom trades detail for size.)
MAPS_BUILD_BASE = "https://build.protomaps.com"
MAP_REGIONS = {
    "USA (lower 48)":        (-125.0, 24.4, -66.9, 49.4, 11),
    "US West":               (-125.0, 31.0, -102.0, 49.4, 11),
    "US Central":            (-104.5, 25.8, -80.5, 49.4, 11),
    "US East":               (-83.0, 24.4, -66.9, 47.5, 11),
    "Arizona":               (-115.0, 31.3, -109.0, 37.1, 12),
    "Phoenix Metro":         (-112.9, 32.9, -111.4, 33.9, 14),
    "California":            (-124.5, 32.5, -114.1, 42.1, 12),
    "Texas":                 (-106.7, 25.8, -93.5, 36.6, 12),
    "Florida":               (-87.7, 24.5, -80.0, 31.1, 12),
    "Colorado":              (-109.1, 36.9, -102.0, 41.1, 12),
    "New Mexico":            (-109.1, 31.3, -103.0, 37.1, 12),
    "Nevada":                (-120.1, 35.0, -114.0, 42.1, 12),
    "Utah":                  (-114.1, 36.9, -109.0, 42.1, 12),
    "Pacific NW (WA/OR)":    (-124.8, 41.9, -116.4, 49.1, 12),
    "Northeast":             (-80.6, 38.9, -66.9, 47.5, 12),
    "Great Lakes":           (-93.0, 37.8, -80.4, 47.6, 12),
    "Southeast":             (-91.7, 30.1, -75.4, 36.7, 12),
}


def latest_pmtiles_build():
    """Newest Protomaps daily build that answers a range request. The dated
    URL rotates (today's isn't published until ~midday UTC), so probe back
    from today. NOTE: the CDN 403s the default Python-urllib User-Agent, so we
    MUST send a curl-like UA -- without it every probe failed and maps looked
    'offline' even with working internet (bailey 2026-07-17)."""
    import urllib.request
    from datetime import timedelta
    for d in range(0, 14):
        day = (datetime.utcnow() - timedelta(days=d)).strftime("%Y%m%d")
        url = "%s/%s.pmtiles" % (MAPS_BUILD_BASE, day)
        try:
            req = urllib.request.Request(url, headers={
                "Range": "bytes=0-0", "User-Agent": "curl/8.5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                if r.status in (200, 206):
                    return url
        except Exception:
            continue
    return None


async def maps_download(state, region, bbox=None, maxzoom=None):
    import shutil
    if not shutil.which("pmtiles"):
        return {"ok": False, "detail": "pmtiles tool not on this image -- reflash the latest build"}
    if region in MAP_REGIONS:
        x1, y1, x2, y2, mz = MAP_REGIONS[region]
        bbox = "%s,%s,%s,%s" % (x1, y1, x2, y2)
        maxzoom = maxzoom or mz
        name = region
    else:
        name = region or "custom"
    if not bbox:
        return {"ok": False, "detail": "no region / bbox"}
    state.broadcast({"type": "maps.progress", "line": "finding latest map build..."})
    src = await asyncio.get_event_loop().run_in_executor(None, latest_pmtiles_build)
    if not src:
        FaultBus_broadcast(state, "maps", "MAP DOWNLOAD: no internet / build unreachable")
        return {"ok": False, "detail": "no reachable Protomaps build (offline?)"}
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_").lower() or "region"
    out = str(MAPS_DIR / (safe + ".pmtiles"))
    state.broadcast({"type": "maps.progress",
                     "line": "extracting %s (bbox %s, z<=%s)..." % (name, bbox, maxzoom or 13)})
    try:
        proc = await asyncio.create_subprocess_exec(
            "pmtiles", "extract", src, out, "--bbox=" + bbox,
            "--maxzoom=" + str(maxzoom or 13),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            txt = line.decode(errors="replace").strip()
            if txt:
                state.broadcast({"type": "maps.progress", "line": txt})
        rc = await proc.wait()
        ok = rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0
        return {"ok": ok, "file": os.path.basename(out) if ok else None,
                "detail": ("saved " + os.path.basename(out)) if ok
                else "extract failed (rc %s)" % rc}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def FaultBus_broadcast(state, fid, label):
    """Push a fault to the frontend fault bus over WS (Part 2 s8)."""
    try:
        state.broadcast({"type": "fault", "id": fid, "label": label})
    except Exception:
        pass


def link_usb_maps():
    """A maps/ (or MAPS/) folder with .pmtiles on a USB stick shows up in NAV
    automatically: symlinked into MAPS_DIR (no copying gigabytes to the SD),
    dead links pruned when the stick is unplugged."""
    try:
        MAPS_DIR.mkdir(parents=True, exist_ok=True)
        for p in MAPS_DIR.iterdir():
            if p.is_symlink() and not p.exists():
                p.unlink()
        for base in (Path("/media"), Path("/mnt")):
            if not base.is_dir():
                continue
            for user_dir in base.iterdir():
                if not user_dir.is_dir():
                    continue
                try:
                    sticks = list(user_dir.iterdir())
                except OSError:
                    continue
                for stick in sticks:
                    for mname in ("maps", "MAPS"):
                        md = stick / mname
                        if not md.is_dir():
                            continue
                        for f in md.glob("*.pmtiles"):
                            dst = MAPS_DIR / f.name
                            if not dst.exists():
                                dst.symlink_to(f)
    except Exception as e:
        log("usb map link failed:", e)


# US holiday / seasonal windows. A channel subfolder whose name matches one of
# these (via _SEASON_ALIASES) auto-takes over that channel while the date is in
# range -- e.g. CH07/Halloween/ plays all October. Same for COMMERCIALS/<name>/.
_SEASON_WINDOWS = {
    "halloween":    lambda m, d: m == 10,
    "thanksgiving": lambda m, d: m == 11 and d <= 30,
    "christmas":    lambda m, d: m == 12 and d <= 26,
    "newyears":     lambda m, d: (m == 12 and d >= 27) or (m == 1 and d <= 2),
    "valentines":   lambda m, d: m == 2 and 7 <= d <= 15,
    "stpatricks":   lambda m, d: m == 3 and 10 <= d <= 18,
    "easter":       lambda m, d: m == 4 and d <= 20,
    "july4th":      lambda m, d: (m == 6 and d >= 28) or (m == 7 and d <= 6),
    "memorial":     lambda m, d: m == 5 and d >= 22,
    "labor":        lambda m, d: m == 9 and d <= 8,
    "summer":       lambda m, d: m in (7, 8),
}
_SEASON_ALIASES = {
    "halloween": "halloween", "spooky": "halloween",
    "christmas": "christmas", "xmas": "christmas", "holiday": "christmas", "holidays": "christmas",
    "thanksgiving": "thanksgiving", "turkey": "thanksgiving",
    "newyears": "newyears", "newyear": "newyears", "nye": "newyears",
    "valentines": "valentines", "valentine": "valentines",
    "stpatricks": "stpatricks", "stpatrick": "stpatricks", "stpatty": "stpatricks", "irish": "stpatricks",
    "easter": "easter",
    "july4th": "july4th", "fourth": "july4th", "independence": "july4th", "4th": "july4th", "july": "july4th",
    "memorial": "memorial", "labor": "labor", "laborday": "labor",
    "summer": "summer",
}


def _canon_season(folder_name):
    key = re.sub(r"[^a-z0-9]", "", folder_name.strip().lower())
    return _SEASON_ALIASES.get(key)


def active_seasons(now=None):
    """Canonical season names whose date window includes today (usually 0-1)."""
    dt = datetime.fromtimestamp(now if now is not None else time.time())
    return [name for name, win in _SEASON_WINDOWS.items() if win(dt.month, dt.day)]


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
            files, seasons = [], {}
            try:
                for f in sorted(entry.iterdir()):
                    if f.is_dir():
                        sea = _canon_season(f.name)   # CH07/Halloween/ -> seasonal pool
                        if sea:
                            try:
                                seasons.setdefault(sea, []).extend(
                                    sorted(v for v in f.iterdir() if v.suffix.lower() in VIDEO_EXT))
                            except OSError:
                                pass
                    elif f.suffix.lower() in VIDEO_EXT:
                        files.append(f)
            except OSError:
                pass
            ch = channels.setdefault(num, {"name": m.group(2), "path": entry, "files": [], "seasons": {}})
            ch["files"].extend(files)
            for k, v in seasons.items():
                ch["seasons"].setdefault(k, []).extend(v)
    return channels


def scan_commercials():
    """Returns {'base': [...], 'seasons': {name: [...]}}. Holiday spots live in
    COMMERCIALS/<Holiday>/ and are preferred while that holiday is in season."""
    base, seasons = [], {}
    roots = [MEDIA_DIR / "TV" / "COMMERCIALS"] + [r / "TV" / "COMMERCIALS" for r in extra_media_roots()]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for f in sorted(root.iterdir()):
                if f.is_dir():
                    sea = _canon_season(f.name)
                    if sea:
                        try:
                            seasons.setdefault(sea, []).extend(
                                sorted(v for v in f.iterdir() if v.suffix.lower() in VIDEO_EXT))
                        except OSError:
                            pass
                elif f.suffix.lower() in VIDEO_EXT:
                    base.append(f)
        except OSError:
            pass
    return {"base": base, "seasons": seasons}


def effective_commercials(commercials, now=None):
    """Season-aware flat spot list: active holiday spots first, then base."""
    if isinstance(commercials, list):
        return commercials   # legacy shape
    spots = []
    for s in active_seasons(now):
        spots.extend(commercials.get("seasons", {}).get(s, []))
    spots.extend(commercials.get("base", []))
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
    commercials = effective_commercials(commercials, now)   # holiday spots first
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

    # never-programmed channel: continuous pseudo-broadcast, seeded by epoch.
    # A seasonal subfolder (CH07/Halloween/) takes over the rotation in season.
    if ch and (ch.get("files") or ch.get("seasons")):
        files = ch.get("files") or []
        seasons = ch.get("seasons") or {}
        for s in active_seasons(now):
            if seasons.get(s):
                files = seasons[s]   # holiday folder overrides normal rotation
                break
        if not files:
            return {"kind": "static", "reason": "off_air"}
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
             "BAROMETRIC_PRESSURE", "HYBRID_BATTERY_REMAINING", "FUEL_TYPE"]

# Baud rates to try, most-likely first. OBDLink EX defaults to 115200.
OBD_BAUDS = [115200, 230400, 38400, 9600]

# Connection attempts in priority order: (protocol, baud, label). Protocol 6
# (ISO 15765-4 CAN 11-bit / 500 kbps) is forced first -- it's what modern
# Fords (incl. the F-150 PowerBoost, whose ECUs answer on 7E8/7EC/7EE 11-bit
# headers) actually use. Auto-detect is the fallback. 250k protocols (9/10)
# are deliberately NOT forced -- they false-positive as "Car Connected" then
# CAN-ERROR on every real query.
OBD_ATTEMPTS = [
    ("6", 115200, "6=CAN11/500"),
    ("7", 115200, "7=CAN29/500"),
    (None, 115200, "auto"),
    (None, 38400, "auto@38400"),
]


def _reset_adapter(port, baud):
    """Fully drain + reset the ELM327 before python-obd touches it. The
    adapter keeps emitting CAN frames from the prior session; that stale data
    desyncs python-obd's ATE0/ATH1 init and makes every connect fail. We send
    ATZ, then actively DRAIN the port until it goes quiet (not just a single
    buffer flush), so python-obd starts from silence. Best-effort; never
    raises."""
    try:
        import serial
        s = serial.Serial(port, baud, timeout=0.4)
        try:
            s.write(b"\r")
            time.sleep(0.2)
            s.reset_input_buffer()
            s.write(b"ATZ\r")          # full adapter reset
            time.sleep(1.5)            # ATZ takes ~1s to complete
            # Drain everything the reset + any stale frames produce, until the
            # port stays silent for a full read cycle.
            deadline = time.time() + 1.5
            while time.time() < deadline:
                n = s.in_waiting
                if n:
                    s.read(n)
                    time.sleep(0.1)
                else:
                    time.sleep(0.15)
                    if not s.in_waiting:
                        break
            s.reset_input_buffer()
        finally:
            s.close()
        time.sleep(0.4)               # let the OS release the port cleanly
    except Exception as e:
        log("OBD: pre-reset skipped:", e)


# PID name -> (OBD request hex, decoder). Decoder takes the list of data
# bytes after the "41 xx" header and returns a plain float in the SAME units
# python-obd uses (so derive()/the frontend are unchanged). Plain floats are
# also JSON-serializable, unlike python-obd's pint Quantity objects.
RAW_PIDS = {
    "SPEED": ("010D", lambda b: float(b[0]) if b else None),                     # km/h
    "RPM": ("010C", lambda b: (256 * b[0] + b[1]) / 4.0 if len(b) >= 2 else None),
    "INTAKE_PRESSURE": ("010B", lambda b: float(b[0]) if b else None),           # kPa
    "MAF": ("0110", lambda b: (256 * b[0] + b[1]) / 100.0 if len(b) >= 2 else None),
    "FUEL_LEVEL": ("012F", lambda b: b[0] * 100.0 / 255 if b else None),
    "COOLANT_TEMP": ("0105", lambda b: float(b[0] - 40) if b else None),         # C
    "CONTROL_MODULE_VOLTAGE": ("0142", lambda b: (256 * b[0] + b[1]) / 1000.0 if len(b) >= 2 else None),
    "OIL_TEMP": ("015C", lambda b: float(b[0] - 40) if b else None),
    "BAROMETRIC_PRESSURE": ("0133", lambda b: float(b[0]) if b else None),
    "HYBRID_BATTERY_REMAINING": ("015B", lambda b: b[0] * 100.0 / 255 if b else None),
    # SAE J1979 fuel-type coding: 1=gasoline 4=diesel 8=electric 17-22=hybrid.
    # Lets detect_vtype() trust the ECU's own word instead of guessing.
    "FUEL_TYPE": ("0151", lambda b: float(b[0]) if b else None),
}


class RawOBD:
    """Minimal, robust ELM327 reader that talks the adapter directly instead
    of through python-obd (whose ATE0/ATH1 init desyncs on this truck). It
    forces a protocol, turns headers/echo off, and parses standard mode-01
    responses ('41 0C 1A F8' -> RPM) itself. Returns plain floats."""

    def __init__(self, port, baud, protocol):
        import serial
        self.ser = serial.Serial(port, baud, timeout=1)
        self.ser.write(b"\r")
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        # NOTE: spaces are left ON (no ATS0) -- the response parser tokenises
        # on whitespace ('41 0C 1A F8'). Sending ATS0 would collapse that to
        # '410C1AF8' and every read would silently return None.
        # ATST32 (~200ms): halve the adapter's hold-the-line timeout; combined
        # with the ' 1' expected-response hint in query_pid, per-PID latency
        # drops from ~600ms to ~50ms => the dashboard updates several times a
        # second instead of every 2-3s (bailey: "pretty slow on the update").
        for cmd, wait in (("ATZ", 1.2), ("ATE0", 0.3), ("ATL0", 0.3),
                          ("ATH0", 0.3),
                          ("ATSP" + (protocol or "0"), 0.3),
                          ("ATST32", 0.3)):
            self._cmd(cmd, wait)
        # The FIRST query after ATSP triggers the ELM327 protocol search,
        # which on a live vehicle can take 4-5s and returns "SEARCHING...".
        # Give it a long read window so the bus actually links before we
        # judge the connection.
        self._cmd("0100", 0.5, max_read=8.0)

    def _cmd(self, s, wait=0.25, max_read=3.0):
        self.ser.reset_input_buffer()
        self.ser.write((s + "\r").encode())
        if wait:
            time.sleep(wait)   # only init commands (ATZ etc.) need a settle
        buf = b""
        end = time.time() + max_read
        while time.time() < end:
            n = self.ser.in_waiting
            if n:
                buf += self.ser.read(n)
                if b">" in buf:   # ELM327 prompt = response complete
                    break
            else:
                time.sleep(0.01)  # tight poll: responses land in tens of ms
        return buf.decode(errors="ignore")

    @staticmethod
    def _hex_tokens(resp):
        """Extract 2-hex-char byte tokens from an ELM327 response, robust to
        whether the adapter has spaces ON ('41 0C 1A F8') or OFF
        ('410C1AF8') -- long hex runs are chunked into byte pairs."""
        out = []
        for t in resp.replace(">", " ").replace("\r", " ").replace("\n", " ").split():
            if not t or len(t) % 2 or any(c not in "0123456789ABCDEFabcdef" for c in t):
                continue
            out.extend(t[i:i + 2].upper() for i in range(0, len(t), 2))
        return out

    def query_pid(self, req):
        """req like '010C' -> list of data bytes, or None.

        The trailing ' 1' is the ELM327 expected-response-count hint: the
        adapter returns as soon as ONE ECU answers instead of holding the
        line for the full ATST timeout waiting for more (the truck has 3
        ECUs echoing each PID; any one of them carries the same value).
        This is the single biggest latency win: ~600ms -> ~50ms per PID."""
        resp = self._cmd(req + " 1", 0.0, max_read=2.0)
        up = resp.upper()
        if "NO DATA" in up or "ERROR" in up or "UNABLE" in up or "STOPPED" in up or "?" in up:
            return None
        resp_mode = "%02X" % (int(req[:2], 16) + 0x40)  # 01 -> 41
        pid_byte = req[2:4].upper()
        toks = self._hex_tokens(resp)
        for i in range(len(toks) - 1):
            if toks[i] == resp_mode and toks[i + 1] == pid_byte:
                try:
                    return [int(x, 16) for x in toks[i + 2:]]
                except ValueError:
                    return None
        return None

    def read(self, pid_name):
        spec = RAW_PIDS.get(pid_name)
        if not spec:
            return None
        req, decode = spec
        try:
            b = self.query_pid(req)
            return decode(b) if b else None
        except Exception:
            return None

    def alive(self):
        return self.read("RPM") is not None or self.read("SPEED") is not None

    @staticmethod
    def _decode_dtc(a, b):
        letter = "PCBU"[(a & 0xC0) >> 6]
        return f"{letter}{(a & 0x30) >> 4}{a & 0x0F:X}{(b & 0xF0) >> 4:X}{b & 0x0F:X}"

    def _parse_dtc_line(self, toks, mode_byte, letter_default):
        """Parse one ECU's mode-03/07/0A response line into codes."""
        try:
            idx = next(i for i, t in enumerate(toks) if t.upper() == mode_byte)
        except StopIteration:
            return []
        rest = toks[idx + 1:]
        if len(rest) % 2 == 1:   # leading DTC-count byte (CAN 11-bit) -> skip
            rest = rest[1:]
        out = []
        for i in range(0, len(rest) - 1, 2):
            a, b = int(rest[i], 16), int(rest[i + 1], 16)
            if a == 0 and b == 0:
                continue
            out.append(self._decode_dtc(a, b))
        return out

    def read_dtcs(self):
        """Read ALL trouble codes the truck stores, across all responding ECUs:
        mode 03 (stored/confirmed -- MIL on), 07 (pending -- seen this drive
        cycle, MIL not yet on) and 0A (permanent -- can't be cleared until the
        ECU re-verifies). The F-150 answers on 3 ECUs, each on its own line;
        earlier we only read the FIRST line of mode 03, so pending/permanent
        codes and other-ECU codes never showed (bailey: 'not displaying past
        codes'). Returns [(code, kind), ...]."""
        seen = {}
        for mode_byte, kind in (("43", "stored"), ("47", "pending"), ("4A", "permanent")):
            cmd = {"43": "03", "47": "07", "4A": "0A"}[mode_byte]
            resp = self._cmd(cmd, 0.4)
            up = resp.upper()
            if "NO DATA" in up or "ERROR" in up or "UNABLE" in up:
                continue
            # Each ECU replies on its own line -> parse every line, not just one.
            for line in resp.replace("\r", "\n").split("\n"):
                for code in self._parse_dtc_line(self._hex_tokens(line), mode_byte, None):
                    seen.setdefault(code, kind)   # first-seen kind wins
        return [(c, k) for c, k in seen.items()]

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def _obd_capture_dir():
    """FAT boot partition dir so captures are pullable from a PC / over SSH."""
    for base in ("/boot/firmware", "/boot"):
        try:
            if os.path.isdir(base) and os.access(base, os.W_OK):
                d = base + "/gost-obd"
                os.makedirs(d, exist_ok=True)
                return d
        except Exception:
            continue
    d = str(STATE_DIR / "obd-capture")
    os.makedirs(d, exist_ok=True)
    return d


# Ford diagnostic module request headers (11-bit CAN). Doors/body live on the
# BCM, NOT the PCM -- earlier probes only hit the PCM (default addressing), so
# they never asked the right module (bailey: door test showed nothing). We set
# ATSH <header> before each module's DID sweep. The gateway may still block
# non-emissions modules, but this is the legitimate way to reach the BCM.
_FORD_MODULES = {
    "PCM": "7E0",   # powertrain (emissions -- always reachable)
    "BCM": "726",   # body control -- DOOR AJAR, lighting, locks
    "IPC": "720",   # instrument cluster (also mirrors some body state)
    "ABS": "760",   # ABS/TPMS on some Fords
}
# Curated mode-22 DID candidates; the per-module range sweep fills the gaps.
_FORD_DIDS = ["402A", "402B", "4028", "4029", "2000", "2001", "1E12",
              "DD00", "DD01", "DD04", "DD05", "8412", "F40C", "D111", "D112"]


def _deep_probe(port, baud):
    """Blocking serial probe (runs in an executor). Returns (summary, logtext)."""
    lines = []
    supported = []
    raw = RawOBD(port, baud, "6")

    def c(cmd, wait=0.0, rd=1.5):
        return raw._cmd(cmd, wait, rd).replace("\r", " ").strip()

    lines.append("# GOST OBD deep capture %s" % datetime.now().isoformat())
    lines.append("# proto 6 (ISO 15765-4 CAN 11-bit/500k), headers on")
    c("ATH1", 0.2)   # headers on so we see which ECU answers

    # ---- supported PIDs (union of the 3 ECUs' bitmasks) ----
    for base, sc in ((0, "0100"), (0x20, "0120"), (0x40, "0140"), (0x60, "0160")):
        resp = c(sc, 0.0, 2.0)
        lines.append("%s -> %s" % (sc, resp))
        toks = RawOBD._hex_tokens(resp)
        for i in range(len(toks) - 5):
            if toks[i].upper() == "41" and int(toks[i + 1], 16) == base:
                try:
                    mask = int("".join(toks[i + 2:i + 6]), 16)
                except Exception:
                    continue
                for b in range(32):
                    if mask & (1 << (31 - b)):
                        supported.append(base + b + 1)
    supported = sorted(set(supported))
    lines.append("# supported mode-01 PIDs: " +
                 " ".join("%02X" % p for p in supported))
    for pid in supported:
        r = c("01%02X" % pid, 0.0, 1.2)
        lines.append("  01%02X -> %s" % (pid, r))

    # ---- mode-22 Ford DID sweep, PER MODULE (addresses the BCM where doors
    # live, not just the PCM) ----
    hits = 0
    dids = _FORD_DIDS + ["%04X" % n for n in range(0x2000, 0x2030)] + \
        ["%04X" % n for n in range(0x4020, 0x4040)]
    for mod, hdr in _FORD_MODULES.items():
        sh = c("ATSH" + hdr, 0.2)   # set request header to this module
        # does the module answer AT ALL? (a supported/unsupported reply both
        # prove it's reachable through the gateway)
        probe = c("221E12", 0.0, 1.0)
        reachable = bool(probe) and "NO DATA" not in probe.upper() and "?" not in probe
        lines.append("# --- module %s (hdr %s) %s ---" % (mod, hdr,
                     "REACHABLE" if reachable else "no answer (gated?)"))
        if not reachable:
            continue
        for did in dids:
            r = c("22" + did, 0.0, 0.8)
            up = r.upper()
            if r and "NO DATA" not in up and "ERROR" not in up and "?" not in r and "7F 22" not in up:
                lines.append("  [%s] 22%s -> %s" % (mod, did, r))
                hits += 1
    c("ATSH7E0", 0.2)   # restore default header

    # ---- DTCs, all types ----
    for cmd, kind in (("03", "stored"), ("07", "pending"), ("0A", "permanent")):
        lines.append("MODE %s (%s) -> %s" % (cmd, kind, c(cmd, 0.0, 2.0)))

    # ---- short passive sample (confirms the port is gateway-gated) ----
    raw.ser.reset_input_buffer()
    raw.ser.write(b"ATMA\r")
    import time as _t
    buf = b""
    end = _t.time() + 5
    while _t.time() < end:
        n = raw.ser.in_waiting
        if n:
            buf += raw.ser.read(n)
        else:
            _t.sleep(0.01)
    raw._cmd("", 0.1, 0.5)   # any char stops ATMA
    frames = [l for l in buf.decode(errors="replace").replace("\r", "\n").split("\n") if l.strip() and l.strip() != ">"]
    lines.append("# passive ATMA 5s: %d frames (0 = gateway-gated, expected)" % len(frames))
    lines.extend("  " + f for f in frames[:40])
    raw.close()

    summary = ("CAPTURE DONE\nsupported PIDs: %d\nmode-22 hits: %d\npassive frames: %d"
               % (len(supported), hits, len(frames)))
    return summary, "\n".join(lines) + "\n"


def _mscan_probe(port, baud):
    """MS-CAN body probe (executor). Switches an OBDLink EX/MX+ to Ford MS-CAN
    (OBD pins 3/11, 125 kbps) -- the bus a normal HS-CAN ELM327 can't reach where
    door/TPMS often live -- then passively sniffs it (headers on) and pokes the
    BCM, logging everything so the door/TPMS frames can be reverse-engineered.
    EXPERIMENTAL: the MS-CAN init + BCM addressing are best-guesses to be dialled
    in against the real truck (a 2021 may route body data on gatewayed HS-CAN
    instead). Returns (summary, logtext)."""
    import time as _t
    lines = []
    raw = RawOBD(port, baud, "6")   # opens the port; we override the protocol below

    def c(cmd, wait=0.25, rd=0.5):
        return raw._cmd(cmd, wait, rd).replace("\r", " ").strip()

    lines.append("# GOST MS-CAN / BODY probe %s" % datetime.now().isoformat())
    lines.append("# needs an OBDLink EX/MX+ (STN chip). Switching to Ford MS-CAN.")
    # Ford MS-CAN = ISO 15765 11-bit @ 125 kbps; ATSP B + ATPB 40 08. Headers ON
    # so ATMA shows each frame's arbitration ID.
    for cmd in ("ATZ", "ATE0", "ATL0", "ATS1", "ATH1", "ATSP B", "ATPB 40 08"):
        lines.append("%-10s -> %s" % (cmd, c(cmd, 0.5 if cmd == "ATZ" else 0.25)))

    # ---- passive monitor: dump every MS-CAN broadcast frame for ~8s ----
    lines.append("# --- passive ATMA 8s: OPEN/CLOSE A DOOR and note tire PSI NOW ---")
    raw.ser.reset_input_buffer()
    raw.ser.write(b"ATMA\r")
    buf, end = b"", _t.time() + 8
    while _t.time() < end:
        n = raw.ser.in_waiting
        if n:
            buf += raw.ser.read(n)
        else:
            _t.sleep(0.01)
    raw._cmd("", 0.1, 0.5)   # any char stops ATMA
    frames = [l for l in buf.decode(errors="replace").replace("\r", "\n").split("\n")
              if l.strip() and l.strip() != ">"]
    lines.append("# MS-CAN passive frames: %d" % len(frames))
    lines.extend("  " + f for f in frames[:150])

    # ---- active: poke the BCM/SJB directly on MS-CAN (candidate headers + DIDs) ----
    lines.append("# --- active BCM query on MS-CAN (candidate headers/DIDs) ---")
    for hdr in ("726", "737", "760"):
        c("ATSH" + hdr, 0.25)
        for did in ("22DD00", "22DD01", "224028", "22411F"):
            lines.append("  %s@%s -> %s" % (did, hdr, c(did, 0.35, 0.6)))
    raw.close()

    summary = ("MS-CAN PROBE DONE\npassive frames: %d\n"
               "  0 = MS-CAN empty/absent (this 2021 likely routes body data on\n"
               "      gatewayed HS-CAN) -- OBD dead-end, need a CAN tap or sensors.\n"
               "  >0 = there's traffic we can decode -- share the log." % len(frames))
    return summary, "\n".join(lines) + "\n"


def _try_raw(port, protocol, baud):
    """Return a linked RawOBD (RPM OR speed answering) or None. Retries a few
    times because the bus can need a beat to settle right after the protocol
    search completes. Runs in an executor."""
    raw = None
    try:
        raw = RawOBD(port, baud, protocol)
        for _ in range(4):
            if raw.read("RPM") is not None or raw.read("SPEED") is not None:
                return raw
            time.sleep(0.4)
        raw.close()
    except Exception as e:
        log("OBD raw: error", protocol, ":", e)
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass
    return None

# Engine is considered "running" at/above this RPM. Below it (≈0) on a hybrid
# means the gas engine has shut off and the truck is on battery. Ford's V6
# idles ~600-700 RPM, so anything under ~250 is engine-off, not a low idle.
ENGINE_ON_RPM = 250.0


def classify_drive_mode(rpm, speed_mph):
    """Derive what an F-150 PowerBoost (or any hybrid) is doing from just the
    two universal OBD2 PIDs, so we can show engine-vs-battery without any
    Ford-specific PIDs:

      ENGINE  -- gas engine running (RPM above threshold)
      EV      -- moving with the engine off (battery drive)
      IDLE-EV -- stopped with the engine off (electric hold / stop-start)
      STOP    -- stopped, engine running
      None    -- no RPM data (can't tell)

    rpm: engine RPM (may be 0 when on battery), speed_mph: vehicle speed.
    """
    if rpm is None:
        return None
    moving = (speed_mph or 0) > 1.0
    if rpm >= ENGINE_ON_RPM:
        return "ENGINE" if moving else "STOP"
    return "EV" if moving else "IDLE-EV"


class Telemetry:
    def __init__(self, state):
        self.state = state
        self.conn = None
        self.vtype = None
        self.values = {}
        self.derived = {}
        self.connected = False   # showing ANY data (real OR demo fallback)
        self.live = False        # real OBD data is flowing (drives "LIVE" badge)
        self.obd_linked = False  # a real adapter connection is open
        self.raw = None          # RawOBD instance when using the direct reader
        self.use_raw = False     # True => read via RawOBD, not python-obd
        self.demo_t0 = time.time()
        self.obd_status = "idle"
        self.obd_port = ""
        self.last_good = 0.0      # time of the last cycle that read ANY real PID
        self.diag_pause = False   # poll loop yields the port during on-screen diag
        self.dtc_path = STATE_DIR / "dtcs.json"
        self.dtcs = {}
        if self.dtc_path.exists():
            try:
                self.dtcs = json.loads(self.dtc_path.read_text())
            except Exception:
                self.dtcs = {}

    def _close_obd(self):
        """Close whichever OBD connection is open (raw or python-obd)."""
        if self.raw is not None:
            try:
                self.raw.close()
            except Exception:
                pass
            self.raw = None
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        self.use_raw = False

    async def run_diag(self):
        """On-screen OBD diagnostic: pause polling, sweep protocols with the
        raw reader, and return human-readable lines showing exactly what the
        adapter/truck answer -- so the operator can diagnose from the DSI
        touchscreen without any SSH."""
        import glob
        loop = asyncio.get_event_loop()
        self.diag_pause = True
        await asyncio.sleep(0.6)   # let poll_loop notice + release the port
        self._close_obd()
        try:
            ports = sorted(glob.glob("/dev/rfcomm*")) + \
                sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
            lines = [f"PORTS: {', '.join(ports) if ports else 'NONE FOUND'}"]
            if not ports:
                lines.append("No adapter. USB: check cable. Bluetooth MX+: run")
                lines.append("  sudo obd-bt-pair.sh   (once, in the truck) then reboot.")
                return "\n".join(lines)
            port = ports[0]
            for proto in ("6", "7", "8", None):
                tag = proto or "auto"
                await loop.run_in_executor(None, _reset_adapter, port, 115200)

                def probe(pr=proto):
                    try:
                        raw = RawOBD(port, 115200, pr)
                        rpm = raw.read("RPM")
                        spd = raw.read("SPEED")
                        r010c = raw._cmd("010C", 0.2, 5.0).replace("\r", " ").strip()
                        raw.close()
                        return (rpm, spd, r010c)
                    except Exception as e:
                        return (None, None, f"ERROR {e}")

                rpm, spd, r010c = await loop.run_in_executor(None, probe)
                mark = "  <== WORKS" if rpm is not None else ""
                lines.append(f"proto {tag}: RPM={rpm} SPEED={spd}{mark}")
                lines.append(f"   010C raw: {r010c[:46] or '(nothing)'}")
                if rpm is not None:
                    lines.append("LINK OK -- set SOURCE=AUTO; DRIVE will show LIVE.")
                    break
            else:
                lines.append("No protocol read the truck. Ensure key is in RUN")
                lines.append("(engine running). Send this text to Claude.")
            return "\n".join(lines)
        finally:
            self.diag_pause = False

    async def run_capture(self):
        """Deep OBD capture, triggered from the DRIVE tab (bailey's idea: bake
        it into the OS so no SSH/stop-wedge). Pauses the poll loop, takes the
        port, and gathers everything the (gateway-gated) port WILL give via
        request/response -- supported PIDs + values, a mode-22 Ford DID sweep
        (door/oil-life/TPMS candidates), all DTC types, and a short passive
        ATMA sample -- then writes a log to the FAT boot partition so it can be
        pulled from a PC or over SSH. Returns a short on-screen summary."""
        loop = asyncio.get_event_loop()
        self.diag_pause = True
        await asyncio.sleep(0.6)
        self._close_obd()
        try:
            port = self.obd_port or "/dev/ttyUSB0"
            summary, logtext = await loop.run_in_executor(None, _deep_probe, port, 115200)
            path = _obd_capture_dir() + "/capture-%d.log" % int(time.time())
            try:
                with open(path, "w") as f:
                    f.write(logtext)
            except Exception as e:
                path = "(write failed: %s)" % e
            return summary + "\n\nSAVED: " + path + \
                "\nPull it from the boot drive's gost-obd folder, or tell Claude to SSH."
        except Exception as e:
            return "capture failed: %s" % e
        finally:
            self.diag_pause = False

    async def run_mscan(self):
        """MS-CAN body probe (OBDLink EX/MX+): switch to Ford MS-CAN, sniff it and
        poke the BCM to try reaching door/TPMS the HS-CAN port can't. Logs to the
        boot partition. Experimental -- for reverse-engineering on the truck."""
        loop = asyncio.get_event_loop()
        self.diag_pause = True
        await asyncio.sleep(0.6)
        self._close_obd()
        try:
            port = self.obd_port or "/dev/ttyUSB0"
            summary, logtext = await loop.run_in_executor(None, _mscan_probe, port, 115200)
            path = _obd_capture_dir() + "/mscan-%d.log" % int(time.time())
            try:
                with open(path, "w") as f:
                    f.write(logtext)
            except Exception as e:
                path = "(write failed: %s)" % e
            return summary + "\n\nSAVED: " + path
        except Exception as e:
            return "MS-CAN probe failed: %s" % e
        finally:
            self.diag_pause = False

    async def run_watch(self, seconds=45):
        """LIVE DOOR TEST (bailey's idea): stream changing signals to the
        VEHICLE screen so opening a door gives instant visual confirmation.
        Watches only the mode-22 Ford DIDs that actually answer (request/
        response gets through the gateway; passive doesn't), polling them fast
        and broadcasting any value change as it happens."""
        loop = asyncio.get_event_loop()
        self.diag_pause = True
        await asyncio.sleep(0.6)
        self._close_obd()
        raw = None
        try:
            port = self.obd_port or "/dev/ttyUSB0"
            raw = await loop.run_in_executor(None, lambda: RawOBD(port, 115200, "6"))
            await loop.run_in_executor(None, lambda: raw._cmd("ATH1", 0.2))

            def rd(cmd):
                return raw._cmd(cmd, 0.0, 0.8).replace("\r", " ").strip()

            # Build the watch set across Ford MODULES -- crucially the BCM
            # (doors), not just the PCM. Each entry is (label, header, didcmd);
            # only DIDs that actually answer stay in.
            dids = _FORD_DIDS + ["%04X" % n for n in range(0x4020, 0x4030)]
            watch = []   # (label, hdr, "22XXXX")
            for mod, hdr in _FORD_MODULES.items():
                await loop.run_in_executor(None, rd, "ATSH" + hdr)
                for did in dids:
                    r = await loop.run_in_executor(None, rd, "22" + did)
                    up = r.upper()
                    if r and "NO DATA" not in up and "7F 22" not in up and "?" not in r and "ERROR" not in up:
                        watch.append(("%s:%s" % (mod, did), hdr, "22" + did))
            if not watch:
                self.state.broadcast({"type": "probe", "done": True, "changes": 0,
                    "status": "No module answered a body DID (PCM/BCM/IPC/ABS) -- door data is gated on this truck's OBD port. Confirmed not reachable."})
                return "No Ford module answered a body DID -- door status isn't reachable through this OBD port (gateway)."
            self.state.broadcast({"type": "probe",
                "status": "WATCHING %d signal(s) across modules -- open/close a door now" % len(watch), "n": len(watch)})

            def rd_hdr(hdr, cmd):
                raw._cmd("ATSH" + hdr, 0.0, 0.4)
                return raw._cmd(cmd, 0.0, 0.8).replace("\r", " ").strip()

            base = {}
            for label, hdr, cmd in watch:
                base[label] = await loop.run_in_executor(None, rd_hdr, hdr, cmd)
            end = time.time() + seconds
            changes = 0
            while time.time() < end:
                for label, hdr, cmd in watch:
                    cur = await loop.run_in_executor(None, rd_hdr, hdr, cmd)
                    if cur != base.get(label):
                        changes += 1
                        self.state.broadcast({"type": "probe",
                            "changed": {"label": label, "old": base.get(label, ""), "new": cur}})
                        base[label] = cur
                self.state.broadcast({"type": "probe",
                    "status": "%ds left -- %d change(s) seen" % (int(end - time.time()), changes)})
                await asyncio.sleep(0.05)
            self.state.broadcast({"type": "probe", "done": True, "changes": changes})
            return "Door test done: %d change(s) across %d signal(s)." % (changes, len(watch))
        except Exception as e:
            self.state.broadcast({"type": "probe", "done": True, "status": "error: %s" % e})
            return "watch failed: %s" % e
        finally:
            if raw is not None:
                try:
                    await loop.run_in_executor(None, raw.close)
                except Exception:
                    pass
            self.diag_pause = False

    async def connect_obd(self):
        """Try hard to link a real OBDLink/ELM327 adapter across every likely
        port and baud rate, with retries. Returns True and sets obd_linked on
        success. Verbose logging so the journal shows exactly what happened."""
        try:
            import obd
        except ImportError:
            self.obd_status = "python-obd not installed"
            return False
        import glob
        # /dev/rfcomm0 = OBDLink MX+ over Bluetooth SPP, provided by the
        # supervised obd-rfcomm.service (Part 1). Tried FIRST: it's the new
        # primary adapter. USB ELM327s (OBDLink EX = ttyUSB*, native-CDC =
        # ttyACM*) remain as fallback so both transports work from one build.
        ports = sorted(glob.glob("/dev/rfcomm*")) + \
            sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        if not ports:
            self.obd_linked = False
            self.obd_status = "no adapter found (no /dev/rfcomm*, ttyUSB* or ttyACM*)"
            log("OBD:", self.obd_status)
            return False

        loop = asyncio.get_event_loop()
        port = ports[0]
        self.obd_port = port
        last_state = ""

        # --- RAW reader first: it bypasses python-obd's fragile init, which
        # desyncs on this truck. Try the truck's protocol (6), then 7, then
        # auto. If RPM answers, use the raw path -- most robust. ---
        for proto, plabel in (("6", "raw-6"), ("7", "raw-7"), (None, "raw-auto")):
            self.obd_status = f"linking {port} raw proto {proto or 'auto'}..."
            log("OBD:", self.obd_status)
            await loop.run_in_executor(None, _reset_adapter, port, 115200)
            raw = await loop.run_in_executor(None, _try_raw, port, proto, 115200)
            if raw is not None:
                self.raw = raw
                self.use_raw = True
                self.conn = None
                self.connected = True
                self.live = True
                self.obd_linked = True
                rpm0 = raw.read("RPM")
                # NOTE: no live values baked into this string -- it's shown on
                # the DRIVE banner and a frozen "[RPM=0.0]" from connect time
                # reads like a stuck gauge (bailey saw exactly that).
                self.obd_status = f"LIVE {port} raw proto {proto or 'auto'}"
                log("OBD: CONNECTED (raw) --", self.obd_status)
                try:
                    (STATE_DIR / "obd.json").write_text(json.dumps(
                        {"port": port, "baud": 115200, "protocol": proto, "raw": True}))
                except Exception:
                    pass
                return True

        # --- python-obd fallback (kept in case raw ever misbehaves) ---
        # If obd_autodetect (or a prior successful link) saved a known-good
        # config, try it FIRST -- makes subsequent boots link on the first
        # shot instead of re-sweeping protocols.
        attempts = list(OBD_ATTEMPTS)
        try:
            saved = json.loads((STATE_DIR / "obd.json").read_text())
            sp, sb = saved.get("protocol"), int(saved.get("baud", 115200))
            first = (sp, sb, f"saved:{sp or 'auto'}")
            if first not in attempts:
                attempts.insert(0, first)
            if saved.get("port"):
                port = saved["port"]
                self.obd_port = port
            log("OBD: using saved config", saved)
        except Exception:
            pass
        for attempt in range(1, 3):
            for proto, baud, label in attempts:
                self.obd_status = f"linking {port} @ {baud} proto {label} (try {attempt})..."
                log("OBD:", self.obd_status)
                # CRITICAL: flush the adapter first. After a prior session the
                # ELM327 keeps streaming CAN frames, which desync python-obd's
                # ATE0/ATH1 init ("did not return OK") and make every connect
                # fail. A raw ATZ + input-buffer flush gives a clean start.
                await loop.run_in_executor(None, _reset_adapter, port, baud)
                try:
                    conn = await loop.run_in_executor(
                        None,
                        lambda pr=proto, b=baud: obd.OBD(
                            portstr=port, baudrate=b, protocol=pr,
                            fast=False, timeout=12, check_voltage=False),
                    )
                except Exception as e:
                    log("OBD: error", port, baud, label, ":", e)
                    continue
                try:
                    state = str(conn.status())
                except Exception:
                    state = ""
                last_state = state or last_state
                # Acceptance = a FORCED RPM query returns a real value. This
                # is the only reliable test on this truck: 0100 (the support
                # list) can return CAN ERROR on the wrong protocol, so trusting
                # it would both false-positive (proto 9) and false-negative
                # (empty list -> we'd read nothing). A live 010C answer proves
                # both the protocol AND that data actually flows.
                try:
                    linked = conn.is_connected()
                except Exception:
                    linked = False
                rpm_val = None
                if linked:
                    try:
                        r = await loop.run_in_executor(
                            None, lambda: conn.query(obd.commands.RPM, force=True))
                        if r is not None and not r.is_null():
                            rpm_val = r.value
                    except Exception:
                        pass
                try:
                    sup = set(c.name for c in conn.supported_commands)
                except Exception:
                    sup = set()
                real = linked and (rpm_val is not None)
                log(f"OBD: {port}@{baud} proto {label} -> status='{state}' "
                    f"linked={linked} RPM={rpm_val} pids={len(sup)} real={real}")
                if real:
                    self.conn = conn
                    self.connected = True
                    self.live = True
                    self.obd_linked = True
                    proto_name = ""
                    try:
                        proto_name = conn.protocol_name() or ""
                    except Exception:
                        pass
                    self.obd_status = f"LIVE {port} @ {baud} [{proto_name}]"
                    log("OBD: CONNECTED --", self.obd_status)
                    # Remember this working combo for instant reconnect next boot.
                    try:
                        (STATE_DIR / "obd.json").write_text(json.dumps(
                            {"port": port, "baud": baud,
                             "protocol": proto if not str(label).startswith("saved") else proto}))
                    except Exception:
                        pass
                    return True
                try:
                    conn.close()
                except Exception:
                    pass
            await asyncio.sleep(0.5)

        self.obd_linked = False
        if last_state and ("Car" in last_state or "OBD" in last_state):
            self.obd_status = (f"linked on {port} but PIDs empty "
                               f"('{last_state}') -- key in RUN + engine on?")
        elif last_state and "ELM" in last_state:
            self.obd_status = (f"adapter OK on {port} but car not answering "
                               f"('{last_state}') -- ignition in RUN?")
        else:
            self.obd_status = (f"adapter on {port} not responding "
                               f"('{last_state or 'no reply'}') -- check cable")
        log("OBD:", self.obd_status)
        return False

    async def _query(self, pid_name):
        loop = asyncio.get_event_loop()
        # Raw path: direct ELM327 read, returns a plain float already.
        if self.use_raw and self.raw is not None:
            try:
                return await loop.run_in_executor(None, self.raw.read, pid_name)
            except Exception:
                return None
        import obd
        cmd = getattr(obd.commands, pid_name, None)
        if cmd is None or self.conn is None:
            return None
        try:
            # force=True: send the PID even if python-obd's supported-list
            # (built from 0100) doesn't include it. On this truck 0100 can
            # return CAN ERROR while the individual PIDs answer fine.
            r = await loop.run_in_executor(
                None, lambda: self.conn.query(cmd, force=True))
            if r is None or r.is_null():
                return None
            # Return a PLAIN FLOAT, not a pint Quantity -- Quantities are not
            # JSON-serializable, so the telemetry broadcast would throw and the
            # frontend would freeze the moment real data arrived.
            return _num(r.value)
        except Exception:
            return None

    async def poll_fast(self):
        got = 0
        for pid in FAST_PIDS:
            v = await self._query(pid)
            if v is not None:
                self.values[pid] = v
                got += 1
        return got

    async def poll_slow(self):
        got = 0
        for pid in SLOW_PIDS:
            v = await self._query(pid)
            if v is not None:
                self.values[pid] = v
                got += 1
        return got

    async def poll_dtc(self):
        loop = asyncio.get_event_loop()
        try:
            codes = []  # list of (code, desc)
            if self.use_raw and self.raw is not None:
                codes = await loop.run_in_executor(None, self.raw.read_dtcs)
            elif self.conn is not None:
                import obd
                r = await loop.run_in_executor(None, self.conn.query, obd.commands.GET_DTC)
                if r is not None and not r.is_null():
                    codes = [(c, d or "") for c, d in (r.value or [])]
            now_iso = datetime.now().isoformat()
            changed = False
            for code, kind in (codes or []):
                if not code:
                    continue
                label = (kind or "").upper()   # STORED / PENDING / PERMANENT
                if code not in self.dtcs:
                    self.dtcs[code] = {"first_seen": now_iso, "desc": label}
                    changed = True
                elif label and self.dtcs[code].get("desc") != label:
                    self.dtcs[code]["desc"] = label
                    changed = True
            if changed:
                self.dtc_path.write_text(json.dumps(self.dtcs, indent=2))
        except Exception as e:
            log("DTC poll failed:", e)

    def detect_vtype(self):
        # Runtime upgrade to hybrid: if we EVER see the truck moving with the
        # engine off, it's a hybrid/EV regardless of which PIDs answered. This
        # catches the F-150 PowerBoost, whose standard hybrid-battery PID Ford
        # often doesn't expose (so it would otherwise mis-detect as plain gas).
        rpm = _num(self.values.get("RPM"))
        speed_kph = _num(self.values.get("SPEED"))
        mph = speed_kph * 0.621371 if speed_kph is not None else 0
        if rpm is not None and rpm < ENGINE_ON_RPM and mph > 1.0:
            self.vtype = "hybrid"
            return
        if self.vtype:
            return
        # The ECU's own declaration beats every heuristic. Raw path yields the
        # SAE code (float); python-obd yields a string like "Diesel".
        ft = self.values.get("FUEL_TYPE")
        fts = str(ft).lower().replace(".0", "") if ft is not None else ""
        if fts:
            if "hybrid" in fts or fts in ("17", "18", "19", "20", "21", "22"):
                self.vtype = "hybrid"
                return
            if fts == "8" or fts == "electric":
                self.vtype = "ev"
                return
            if fts == "4" or "diesel" in fts:
                # a diesel with an HV pack answering is still a hybrid
                self.vtype = "hybrid" if self.values.get("HYBRID_BATTERY_REMAINING") is not None else "diesel"
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
        rpm = _num(self.values.get("RPM"))
        self.derived = {
            "mph": mph,
            "boost_psi": boost_psi,
            "mpg": mpg,
            "coolant_f": (coolant_c * 9 / 5 + 32) if coolant_c is not None else None,
            "oil_f": (oil_c * 9 / 5 + 32) if oil_c is not None else None,
            "drive_mode": classify_drive_mode(rpm, mph),
        }

    def demo_tick(self):
        t = time.time() - self.demo_t0
        self.vtype = "ev"
        self.connected = True
        self.obd_status = "SHOWCASE (simulated telemetry)"
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
            "drive_mode": "EV" if speed_mph > 1 else "IDLE-EV",
        }
        if "P1A42" not in self.dtcs:
            self.dtcs["P1A42"] = {"first_seen": datetime.now().isoformat(),
                                   "desc": "Hybrid battery cell imbalance (demo)"}
        # Cycle the VEHICLE tab's door diagram so showcase shows it off.
        cyc = int(t / 6) % 8
        self.doors = {"fl": cyc == 1, "fr": cyc == 3, "rl": False, "rr": False,
                      "hood": cyc == 5, "trunk": cyc == 7}
        # Simulated TPMS: right-rear runs low half the time so the low-tire
        # glow + PSI callout has something to show.
        low = int(t / 20) % 2 == 1
        self.tires = {"fl": 36.0, "fr": 35.5, "rl": 36.0,
                      "rr": 27.5 if low else 34.0}

    async def poll_loop(self):
        last_slow = 0.0
        last_dtc = 0.0
        last_connect = 0.0
        dead_cycles = 0
        while True:
            try:
                # On-screen diagnostic owns the port -- stand down.
                if self.diag_pause:
                    if self.raw or self.conn:
                        self._close_obd()
                    await asyncio.sleep(0.3)
                    continue

                mode = self.state.config.get("source_mode", "AUTO")

                # Explicit SHOWCASE mode -- always simulated, never real.
                if mode == "DEMO":
                    if self.obd_linked or self.raw or self.conn:
                        self._close_obd()   # release the port for showcase
                    self.demo_tick()
                    self.live = False
                    self.obd_linked = False
                    await asyncio.sleep(0.2)
                    continue

                # AUTO mode: real OBD data ALWAYS wins. Only when a real link
                # can't be established do we fall back to SHOWCASE so the
                # screen stays alive -- and we keep retrying in the background,
                # switching to real data the instant the adapter links up.
                if not self.obd_linked:
                    now = time.time()
                    # 3s (was 6): halve the showcase gap after a mid-session
                    # drop so a transient USB/BT glitch recovers fast (bailey:
                    # "seen it disconnect mid session"). connect_obd's own
                    # duration keeps it from truly hammering.
                    if now - last_connect > 3:
                        last_connect = now
                        self.values = {}
                        self.vtype = None
                        await self.connect_obd()
                        # Freeze the REAL reason from the connect attempt.
                        # Never re-read obd_status here next cycle -- that fed
                        # the banner back into itself and grew "SHOWCASE
                        # fallback -- SHOWCASE fallback -- ..." forever
                        # (bailey's photo 2026-07-13).
                        self.connect_diag = self.obd_status
                        dead_cycles = 0
                    if not self.obd_linked:
                        self.demo_tick()
                        self.live = False
                        self.obd_status = ("SHOWCASE fallback -- " +
                                           getattr(self, "connect_diag", "searching for adapter..."))
                        await asyncio.sleep(0.2)
                        continue

                # --- real adapter is linked: poll the truck ---
                # INSTANT unplug detection FIRST, before any serial I/O. On USB
                # removal (or a BT rfcomm drop) the /dev node vanishes; checking
                # for it is a non-blocking filesystem stat. This replaces the
                # old alive()-read that ran serial I/O on the event-loop thread
                # -- when the adapter was yanked that read hung the whole async
                # loop, killed the WebSocket keepalive, and left every tab
                # showing "REQUIRES DEVICE" until a reboot (bailey 2026-07-14).
                if self.obd_port and not os.path.exists(self.obd_port):
                    log("OBD: port", self.obd_port, "vanished -- adapter unplugged")
                    self.obd_linked = False
                    self.live = False
                    self._close_obd()
                    self.values = {}
                    self.obd_status = "link lost -- searching..."
                    await asyncio.sleep(0.2)
                    continue
                self.live = True
                # The showcase's FAKE trouble code must never sit in the real
                # ledger (bailey saw demo P1A42 listed like a stored code).
                demo_codes = [c for c, i in self.dtcs.items()
                              if "(demo)" in (i.get("desc") or "")]
                if demo_codes:
                    for c in demo_codes:
                        del self.dtcs[c]
                    try:
                        self.dtc_path.write_text(json.dumps(self.dtcs, indent=2))
                    except Exception:
                        pass
                self.doors = None  # door status needs body-control PIDs (TBD)
                self.tires = None  # TPMS is Ford mode-22, unmapped -- honest null
                got = await self.poll_fast()
                now = time.time()
                if now - last_slow > 2:
                    got += await self.poll_slow()
                    last_slow = now
                if now - last_dtc > 30:
                    await self.poll_dtc()
                    last_dtc = now
                # Unplug / ECU-drop detection: count cycles where NO PID at all
                # responded (a parked truck still answers speed/rpm=0, so this
                # only fires on a genuinely dead link, not a stationary one).
                if got == 0:
                    dead_cycles += 1
                    if dead_cycles > 25:  # ~5s of total silence
                        # Only give up if the adapter actually disconnected -- a
                        # quiet-but-linked ECU must NOT flap back to SHOWCASE.
                        # alive() does serial I/O, so run it in an executor --
                        # never on the event-loop thread (see unplug note above).
                        try:
                            loop = asyncio.get_event_loop()
                            if self.use_raw:
                                still = await loop.run_in_executor(
                                    None, lambda: bool(self.raw and self.raw.alive()))
                            else:
                                still = await loop.run_in_executor(None, self.conn.is_connected)
                        except Exception:
                            still = False
                        if not still:
                            log("OBD: adapter disconnected, re-detecting")
                            self.obd_linked = False
                            self.live = False
                            self._close_obd()
                            self.values = {}
                            self.obd_status = "link lost -- searching..."
                            dead_cycles = 0
                            continue
                        # linked but quiet: keep waiting, note it on screen
                        self.obd_status = "LIVE but ECU quiet (no PID answers yet)"
                        dead_cycles = 0
                else:
                    dead_cycles = 0
                self.detect_vtype()
                self.derive()
            except Exception as e:
                log("telemetry loop error (continuing):", e)
                self._close_obd()
                self.obd_linked = False
                self.live = False
            await asyncio.sleep(0.2)

    def snapshot(self):
        return {
            "type": "telemetry",
            "connected": self.connected,
            "live": self.live,   # True => real OBDLink data (drives LIVE badge)
            "source_mode": self.state.config.get("source_mode", "AUTO"),
            "vtype": self.vtype or "unknown",
            "values": self.values,
            "derived": self.derived,
            "dtcs": self.dtcs,
            "obd_status": self.obd_status,
            "obd_port": self.obd_port,
            "doors": getattr(self, "doors", None),
            "tires": getattr(self, "tires", None),
            "carplay": carplay_dongle(),
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
    def __init__(self, broadcast_cb, state=None):
        self.broadcast_cb = broadcast_cb
        self.state = state

    async def run(self):
        try:
            import serial
        except ImportError:
            log("pyserial unavailable, GPS disabled")
            return
        import glob
        # GPS puck ports. Do NOT touch /dev/serial0 (Pi 5 Bluetooth UART) and
        # NEVER the port the OBD reader is on -- a USB ELM327 also enumerates as
        # ttyUSB*, and the GPS probe opening it fought the OBD link and could
        # wedge the adapter (bailey 2026-07-14). Default to ttyACM* (most USB
        # GPS pucks) + an explicit gps_port; a ttyUSB GPS must be set via
        # gps_port so it never collides with the OBD adapter by accident.
        def cfg_port():
            try:
                return self.state.config.get("gps_port") if self.state else None
            except Exception:
                return None
        def candidates():
            obd_port = ""
            try:
                obd_port = self.state.telemetry.obd_port if self.state else ""
            except Exception:
                obd_port = ""
            cp = cfg_port()
            cand = ([cp] if cp else []) + sorted(glob.glob("/dev/ttyACM*"))
            return [p for p in cand if p and p != obd_port]
        loop = asyncio.get_event_loop()
        while True:
            ports = candidates()
            if not ports:
                await asyncio.sleep(10)   # nothing plugged in -- check back, quietly
                continue
            ser = None
            for p in ports:
                try:
                    ser = serial.Serial(p, 9600, timeout=1)
                    break
                except Exception:
                    ser = None
            if ser is None:
                await asyncio.sleep(10)
                continue
            # Probe: a real GPS emits NMEA ('$G...') within a few seconds. If it
            # doesn't, this isn't a GPS (e.g. an ELM327 on ttyUSB) -- release it
            # and back off instead of erroring forever.
            errs = 0
            saw_nmea = False
            probe_deadline = time.time() + 6
            try:
                while True:
                    line = await loop.run_in_executor(None, ser.readline)
                    text = line.decode(errors="ignore").strip()
                    if text.startswith("$G"):
                        saw_nmea = True
                        fix = parse_nmea(text)
                        if fix:
                            self.broadcast_cb({"type": "gps", **fix})
                        errs = 0
                    elif not saw_nmea and time.time() > probe_deadline:
                        raise IOError("no NMEA -- not a GPS on " + ser.port)
                    elif not text:
                        errs += 1
                        if errs > 5 and not saw_nmea:
                            raise IOError("silent port -- not a GPS on " + ser.port)
            except Exception as e:
                try:
                    ser.close()
                except Exception:
                    pass
                log("GPS: releasing", getattr(ser, "port", "?"), "--", e)
                await asyncio.sleep(15)   # back off; don't spam


class Dashcam:
    """Rolling-buffer recorder: 24 x 5-minute segments (~2h loop) that wrap
    and overwrite the oldest -- an unbounded single file would eventually
    fill the SD card and take the whole OS down with it."""

    def __init__(self):
        self.proc = None

    async def set_enabled(self, on):
        if on and not self.proc:
            out_dir = STATE_DIR / "dashcam"
            out_dir.mkdir(exist_ok=True)
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    "rpicam-vid", "-t", "0",
                    "--segment", "300000",   # 5-minute segments
                    "--wrap", "24",          # reuse seg 01..24 -> ~2h rolling loop
                    "-o", str(out_dir / "seg%02d.h264"))
            except FileNotFoundError:
                log("rpicam-vid not available, dashcam disabled")
                self.proc = None
        elif not on and self.proc:
            if self.proc.returncode is None:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.proc.kill()
            self.proc = None


# --------------------------------------------------------------- wifi/pw ----

async def _wifi_radio_up():
    """Clear soft-blocks, set the regulatory domain, and turn the radio on.
    The kernel regdom defaults to "00" (world), which disables channels and
    makes scans/joins fail -- setting the country is the real fix. Best-effort;
    every step is allowed to fail (e.g. on a dev box with no wlan0)."""
    try:
        with open("/etc/gost-wifi-country") as f:
            country = (f.read().strip() or "US")
    except Exception:
        country = "US"
    for cmd in (["rfkill", "unblock", "wifi"],
                ["iw", "reg", "set", country],
                ["nmcli", "radio", "wifi", "on"]):
        try:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.wait(), timeout=15)
        except Exception:
            pass


async def wifi_scan():
    await _wifi_radio_up()   # empty scan list usually = radio was never brought up
    try:
        # --rescan yes forces a fresh scan; the cache is empty right after the
        # radio comes on, which is why the panel used to show "no networks".
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        # nmcli -t escapes literal colons in a field as "\:"; split on unescaped
        # colons only so SSIDs containing a colon parse correctly.
        best = {}
        for line in out.decode(errors="ignore").splitlines():
            parts = re.split(r"(?<!\\):", line)
            ssid = parts[0].replace("\\:", ":") if parts else ""
            if not ssid:
                continue
            try:
                sig = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            except ValueError:
                sig = 0
            sec = parts[2] if len(parts) > 2 else ""
            # De-dupe: the same SSID appears once per BSS/band; keep the strongest.
            if ssid not in best or sig > best[ssid]["_sig"]:
                best[ssid] = {"ssid": ssid, "signal": str(sig), "security": sec, "_sig": sig}
        nets = sorted(best.values(), key=lambda n: n["_sig"], reverse=True)
        for n in nets:
            n.pop("_sig", None)
        return nets
    except asyncio.TimeoutError:
        log("wifi scan timed out")
        return []
    except Exception as e:
        log("wifi scan failed:", e)
        return []


async def wifi_join(ssid, psk):
    if not ssid:
        return {"ok": False, "detail": "no SSID"}
    try:
        # Radio may be soft-blocked / regdom unset / off; bring it fully up (incl.
        # regulatory domain) or nmcli fails with a useless generic error.
        await _wifi_radio_up()
        try:
            p = await asyncio.create_subprocess_exec(
                "nmcli", "dev", "wifi", "rescan",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.wait(), timeout=15)
        except Exception:
            pass
        args = ["nmcli", "dev", "wifi", "connect", ssid]
        if psk:
            args += ["password", psk]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
        detail = (out or b"").decode(errors="ignore").strip()[:200]
        ok = proc.returncode == 0
        # Polkit denies session-less systemd services ("Not authorized to
        # control networking" -- hit on hardware 2026-07-12). The polkit rule
        # installed by install.sh is the real fix; sudo (whitelisted in
        # sudoers-gost) is the fallback so Wi-Fi works either way.
        if not ok and "authorized" in detail.lower():
            log("wifi join: polkit denied, retrying via sudo")
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
            detail = (out or b"").decode(errors="ignore").strip()[:200]
            ok = proc.returncode == 0
        log("wifi join", ssid, "->", "OK" if ok else "FAIL", detail)
        return {"ok": ok, "detail": detail}
    except asyncio.TimeoutError:
        return {"ok": False, "detail": "timed out (weak signal or wrong password)"}
    except Exception as e:
        log("wifi join failed:", e)
        return {"ok": False, "detail": str(e)}


# Carlinkit-style CarPlay/Android Auto dongles (CPC200-CCPA etc.) enumerate
# as USB vendor 0x1314. The CARPLAY tab only appears when one is plugged in;
# the projection stack itself is a future round (needs the dongle to test).
_CARPLAY_CACHE = {"t": 0.0, "info": None}


def carplay_dongle():
    now = time.time()
    if now - _CARPLAY_CACHE["t"] < 10:
        return _CARPLAY_CACHE["info"]
    info = None
    try:
        base = "/sys/bus/usb/devices"
        for d in os.listdir(base):
            vp = os.path.join(base, d, "idVendor")
            if not os.path.isfile(vp):
                continue
            try:
                with open(vp) as f:
                    vid = f.read().strip().lower()
                if vid != "1314":
                    continue
                with open(os.path.join(base, d, "idProduct")) as f:
                    pid = f.read().strip()
                name = "Carlinkit dongle"
                np = os.path.join(base, d, "product")
                if os.path.isfile(np):
                    with open(np) as f:
                        name = f.read().strip() or name
                info = {"vid": vid, "pid": pid, "name": name}
                break
            except Exception:
                continue
    except Exception:
        pass
    _CARPLAY_CACHE.update(t=now, info=info)
    return info


class TermSession:
    """Persistent bash for the TERM tab. Line-mode (no pty): commands go to
    bash stdin, merged stdout/stderr streams back over WS. Enough for apt,
    map downloads for NAV, cat/sed edits -- not for vim/nano/top."""

    def __init__(self, broadcast):
        self.broadcast = broadcast
        self.proc = None

    async def ensure(self):
        if self.proc is not None and self.proc.returncode is None:
            return
        self.proc = await asyncio.create_subprocess_exec(
            "bash", cwd=os.path.expanduser("~"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        asyncio.get_event_loop().create_task(self._pump())

    async def _pump(self):
        try:
            while True:
                data = await self.proc.stdout.read(4096)
                if not data:
                    self.broadcast({"type": "term.out",
                                    "data": "\n[shell exited -- next command starts a fresh one]\n"})
                    break
                self.broadcast({"type": "term.out", "data": data.decode(errors="replace")})
        except Exception as e:
            log("term pump ended:", e)

    async def run(self, line):
        await self.ensure()
        try:
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()
        except Exception as e:
            self.broadcast({"type": "term.out", "data": f"[term error: {e}]\n"})
            self.proc = None


# ---------------------------------------------------------------- bluetooth --

async def _btctl(*args, timeout=20):
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode == 0, (out or b"").decode(errors="ignore")


_OBD_BT_NAMES = ("obd", "obdlink", "elm", "vgate", "veepeak", "obdii", "obd2")


async def bt_scan():
    """Power on, discover for 8s, return every device with paired/connected
    state so the UI can show what's already linked (like Wi-Fi)."""
    try:
        for cmd in (["rfkill", "unblock", "bluetooth"],):
            try:
                p = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(p.wait(), timeout=5)
            except Exception:
                pass
        await _btctl("power", "on")
        await _btctl("--timeout", "8", "scan", "on", timeout=15)
        ok, out = await _btctl("devices")
        devs = []
        for line in out.splitlines():
            p = line.strip().split(" ", 2)
            if len(p) >= 3 and p[0] == "Device":
                devs.append({"mac": p[1], "name": p[2]})
        # annotate paired/connected (cap the info calls so scan stays snappy)
        for d in devs[:20]:
            _ok, info = await _btctl("info", d["mac"], timeout=8)
            d["paired"] = "Paired: yes" in info
            d["connected"] = "Connected: yes" in info
        # connected devices first
        devs.sort(key=lambda d: (not d.get("connected"), not d.get("paired")))
        return devs
    except Exception as e:
        log("bt scan failed:", e)
        return []


async def _write_obd_bt_conf(mac):
    """Persist a paired OBD adapter so obd-rfcomm.service auto-reconnects on
    every boot (bailey: BT OBD didn't reconnect after a power cycle -- the
    Settings pairing never wrote the config the link service reads)."""
    script = ("mkdir -p /etc/gost && printf 'OBD_BT_MAC=%s\\n"
              "OBD_BT_CHANNEL=auto\\nOBD_RFCOMM_NODE=/dev/rfcomm0\\n' %s "
              "> /etc/gost/obd-bt.conf" % (mac, mac))
    try:
        p = await asyncio.create_subprocess_exec("sudo", "-n", "sh", "-c", script,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), timeout=10)
        p2 = await asyncio.create_subprocess_exec("sudo", "-n", "systemctl", "restart", "obd-rfcomm",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p2.wait(), timeout=10)
        return True
    except Exception as e:
        log("obd-bt.conf write failed:", e)
        return False


async def bt_pair(mac, name=""):
    if not mac:
        return {"ok": False, "detail": "no device"}
    try:
        detail = ""
        for step in ("pair", "trust", "connect"):
            ok, out = await _btctl(step, mac, timeout=40)
            detail = out.strip()[-160:]
            # "connect" often fails for OBD dongles (no audio/input profile);
            # a successful pair+trust is what matters for rfcomm later.
            if not ok and step == "pair" and "already" not in out.lower():
                return {"ok": False, "detail": detail}
        # If this is an OBD adapter, wire it up for auto-reconnect at boot.
        is_obd = any(k in (name or "").lower() for k in _OBD_BT_NAMES)
        if is_obd:
            wrote = await _write_obd_bt_conf(mac)
            detail = ("OBD adapter saved -- will auto-connect on boot" if wrote
                      else "paired, but auto-reconnect config write failed")
        return {"ok": True, "detail": detail, "obd": is_obd}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


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
        self.gps = GPSReader(self.broadcast, self)
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
        link_usb_maps()
        self.channels = scan_channels()
        self.commercials = scan_commercials()
        self.music = scan_music()
        self.podcasts = scan_podcasts()

    def channel_summary(self):
        # User-assigned names (set from the GUIDE tab) override the
        # folder-derived default; the folder on disk is left untouched.
        names = self.config.get("channel_names", {}) or {}
        return {num: {"name": names.get(num, c["name"]),
                      "files": [f.name for f in c["files"]],
                      "durations": {f.name: self.dur_cache.get(f)
                                    for f in c["files"] if self.dur_cache.get(f)},
                      "seasons": {s: [f.name for f in fl]
                                  for s, fl in (c.get("seasons") or {}).items()}}
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
        comm = self.commercials if isinstance(self.commercials, dict) else {"base": self.commercials, "seasons": {}}
        return {
            "type": "library", "channels": self.channel_summary(),
            "music": self._audio_summary(self.music),
            "podcasts": self._audio_summary(self.podcasts),
            "active_seasons": active_seasons(),
            "commercial_seasons": sorted(comm.get("seasons", {}).keys()),
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
        # On the Pi this is the kiosk Chromium; the extra names + webbrowser
        # fallback let it also work on a dev desktop (and report back either way).
        for exe in ("chromium-browser", "chromium", "google-chrome", "chrome"):
            try:
                self.app_proc = await asyncio.create_subprocess_exec(exe, "--kiosk", f"--app={url}")
                return True
            except FileNotFoundError:
                continue
        try:
            import webbrowser
            if webbrowser.open(url):
                log("launched app via default browser:", url)
                return True
        except Exception as e:
            log("webbrowser fallback failed:", e)
        log("no browser found, cannot launch app for", url)
        return False

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

# --------------------------------------------------------- youtube search ----
# CH83 is a browsable YouTube. YouTube blocks iframing its result pages, so the
# kiosk can't scrape them client-side (CORS + X-Frame-Options). We fetch the
# results page server-side here and pull video metadata out of the embedded
# ytInitialData JSON -- no API key, so the .img stays turnkey. Results play in
# the frameable /embed/ player on the frontend.
_YT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Seed terms for CH83 "RANDOM" (lean-back) mode -- picking a random seed and a
# random hit off it gives broad variety without a recommendation API/key.
_YT_SEEDS = [
    "F-150 PowerBoost", "off road trucks", "overlanding", "monster truck",
    "diesel truck build", "car review 2024", "dashcam close calls",
    "classic rock live", "synthwave mix", "guitar solo", "80s music video",
    "space documentary", "nature 4k", "aviation cockpit", "history documentary",
    "how its made", "top 10 facts", "comedy sketch", "cooking recipe",
    "fishing", "national parks", "trending music",
]


def _yt_text(node):
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"] or ""
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return ""


def _yt_collect(node, out):
    """Recursively gather every videoRenderer dict (order-preserving)."""
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId"):
            out.append(vr)
        for v in node.values():
            _yt_collect(v, out)
    elif isinstance(node, list):
        for v in node:
            _yt_collect(v, out)


# innertube config lifted from the results page, needed to fetch continuation
# ("scroll") pages via the same private API youtube.com's own UI uses.
_YT_CFG = {"key": None, "ver": None}


def _yt_build(renderers, seen, limit):
    out = []
    for vr in renderers:
        vid = vr.get("videoId")
        title = _yt_text(vr.get("title"))
        if not vid or vid in seen or not title:
            continue
        seen.add(vid)
        out.append({
            "id": vid,
            "title": title,
            "duration": _yt_text(vr.get("lengthText")),
            "channel": _yt_text(vr.get("ownerText")) or _yt_text(vr.get("longBylineText")),
            "views": _yt_text(vr.get("shortViewCountText")) or _yt_text(vr.get("viewCountText")),
            "thumb": "https://i.ytimg.com/vi/%s/mqdefault.jpg" % vid,
        })
        if len(out) >= limit:
            break
    return out


def _yt_find_continuation(node):
    """The search 'load-more' token. Must come from continuationItemRenderer --
    grabbing any continuationCommand.token instead lands on a Shorts-shelf/menu
    token whose continuation returns Shorts, not more search results."""
    found = []

    def walk(n):
        if found:
            return
        if isinstance(n, dict):
            cir = n.get("continuationItemRenderer")
            if isinstance(cir, dict):
                tok = (((cir.get("continuationEndpoint") or {})
                        .get("continuationCommand") or {}).get("token"))
                if tok:
                    found.append(tok)
                    return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(node)
    return found[0] if found else None


def _yt_fetch_search(query, limit=40):
    url = "https://www.youtube.com/results?" + urlencode(
        {"search_query": query, "hl": "en", "gl": "US"})
    req = urllib.request.Request(url, headers={
        "User-Agent": _YT_UA,
        "Accept-Language": "en-US,en;q=0.9",
        # skip the EU consent interstitial that otherwise replaces the results
        "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+000",
    })
    with urllib.request.urlopen(req, timeout=8) as r:
        html = r.read().decode("utf-8", "replace")
    km = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    vm = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html)
    if km:
        _YT_CFG["key"] = km.group(1)
    if vm:
        _YT_CFG["ver"] = vm.group(1)
    m = re.search(r"ytInitialData\s*=\s*({.+?})\s*;\s*</script>", html)
    if not m:
        m = re.search(r"var\s+ytInitialData\s*=\s*({.+?});", html)
    if not m:
        return {"results": [], "token": None}
    data = json.loads(m.group(1))
    renderers = []
    _yt_collect(data, renderers)
    return {"results": _yt_build(renderers, set(), limit),
            "token": _yt_find_continuation(data)}


def _yt_fetch_continuation(token, limit=40):
    key = _YT_CFG.get("key") or "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
    ver = _YT_CFG.get("ver") or "2.20240401.00.00"
    body = json.dumps({
        "context": {"client": {"clientName": "WEB", "clientVersion": ver,
                               "hl": "en", "gl": "US"}},
        "continuation": token,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/search?key=" + key,
        data=body, headers={"User-Agent": _YT_UA, "Content-Type": "application/json",
                            "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    renderers = []
    _yt_collect(data, renderers)
    return {"results": _yt_build(renderers, set(), limit),
            "token": _yt_find_continuation(data)}


async def youtube_search(query):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _yt_fetch_search, query)
    except Exception as e:
        log("youtube search failed:", e)
        return {"results": [], "token": None}


async def youtube_more(token):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _yt_fetch_continuation, token)
    except Exception as e:
        log("youtube continuation failed:", e)
        return {"results": [], "token": None}


# -------------------------------------------------------------- games/emu ----
# RetroArch front-end. We ship NO ROMs (legal); the user drops their own game
# files in ROMS_DIR (via USB/TERM) and we map extension -> libretro core.
# Unambiguous extension -> (system label, libretro core). Ambiguous disc formats
# (.iso/.cue/.chd...) are resolved by SYSTEM FOLDER instead (RetroPie convention).
_ROM_CORES = {
    ".nes": ("NES", "fceumm"), ".fds": ("FAMICOM DISK", "fceumm"), ".unf": ("NES", "fceumm"),
    ".sfc": ("SUPER NES", "snes9x"), ".smc": ("SUPER NES", "snes9x"),
    ".gb": ("GAME BOY", "gambatte"), ".gbc": ("GAME BOY COLOR", "gambatte"), ".sgb": ("SUPER GAME BOY", "gambatte"),
    ".gba": ("GAME BOY ADVANCE", "mgba"), ".nds": ("NINTENDO DS", "desmume"), ".vb": ("VIRTUAL BOY", "mednafen_vb"),
    ".n64": ("NINTENDO 64", "mupen64plus_next"), ".z64": ("NINTENDO 64", "mupen64plus_next"), ".v64": ("NINTENDO 64", "mupen64plus_next"),
    ".md": ("SEGA GENESIS", "genesis_plus_gx"), ".gen": ("SEGA GENESIS", "genesis_plus_gx"),
    ".smd": ("SEGA GENESIS", "genesis_plus_gx"), ".sms": ("MASTER SYSTEM", "genesis_plus_gx"),
    ".gg": ("GAME GEAR", "genesis_plus_gx"), ".32x": ("SEGA 32X", "picodrive"),
    ".pce": ("PC ENGINE", "mednafen_pce_fast"), ".sgx": ("SUPERGRAFX", "mednafen_pce_fast"),
    ".ngp": ("NEO GEO POCKET", "mednafen_ngp"), ".ngc": ("NEO GEO POCKET", "mednafen_ngp"),
    ".ws": ("WONDERSWAN", "mednafen_wswan"), ".wsc": ("WONDERSWAN COLOR", "mednafen_wswan"),
    ".a26": ("ATARI 2600", "stella"), ".a78": ("ATARI 7800", "prosystem"),
    ".lnx": ("ATARI LYNX", "handy"), ".j64": ("ATARI JAGUAR", "virtualjaguar"),
    ".col": ("COLECOVISION", "bluemsx"), ".int": ("INTELLIVISION", "freeintv"),
    ".d64": ("COMMODORE 64", "vice_x64"), ".t64": ("COMMODORE 64", "vice_x64"), ".adf": ("AMIGA", "puae"),
    ".pbp": ("PLAYSTATION", "pcsx_rearmed"), ".gdi": ("DREAMCAST", "flycast"), ".cdi": ("DREAMCAST", "flycast"),
    ".iso": ("PSP", "ppsspp"), ".cso": ("PSP", "ppsspp"), ".zip": ("ARCADE", "fbneo"), ".7z": ("ARCADE", "fbneo"),
}
# Drop ROMs in roms/<system>/ and the FOLDER decides the console (needed for
# shared extensions: a .cue in roms/psx/ = PlayStation, in roms/segacd/ = Sega CD).
_SYSTEM_FOLDERS = {
    "nes": ("NES", "fceumm"), "snes": ("SUPER NES", "snes9x"), "sfc": ("SUPER NES", "snes9x"),
    "gb": ("GAME BOY", "gambatte"), "gbc": ("GAME BOY COLOR", "gambatte"), "gba": ("GAME BOY ADVANCE", "mgba"),
    "n64": ("NINTENDO 64", "mupen64plus_next"), "nds": ("NINTENDO DS", "desmume"),
    "gc": ("GAMECUBE", "dolphin"), "gamecube": ("GAMECUBE", "dolphin"), "wii": ("WII", "dolphin"),
    "genesis": ("SEGA GENESIS", "genesis_plus_gx"), "megadrive": ("SEGA GENESIS", "genesis_plus_gx"),
    "sms": ("MASTER SYSTEM", "genesis_plus_gx"), "mastersystem": ("MASTER SYSTEM", "genesis_plus_gx"),
    "gamegear": ("GAME GEAR", "genesis_plus_gx"), "gg": ("GAME GEAR", "genesis_plus_gx"),
    "segacd": ("SEGA CD", "genesis_plus_gx"), "sega32x": ("SEGA 32X", "picodrive"), "32x": ("SEGA 32X", "picodrive"),
    "saturn": ("SEGA SATURN", "mednafen_saturn"), "dreamcast": ("DREAMCAST", "flycast"), "dc": ("DREAMCAST", "flycast"),
    "psx": ("PLAYSTATION", "pcsx_rearmed"), "ps1": ("PLAYSTATION", "pcsx_rearmed"), "playstation": ("PLAYSTATION", "pcsx_rearmed"),
    "psp": ("PSP", "ppsspp"), "ps2": ("PLAYSTATION 2", "pcsx2"),
    "pcengine": ("PC ENGINE", "mednafen_pce_fast"), "tg16": ("TURBOGRAFX-16", "mednafen_pce_fast"),
    "neogeo": ("NEO GEO", "fbneo"), "arcade": ("ARCADE", "fbneo"), "mame": ("ARCADE", "mame2003_plus"), "fbneo": ("ARCADE", "fbneo"),
    "atari2600": ("ATARI 2600", "stella"), "atari7800": ("ATARI 7800", "prosystem"),
    "lynx": ("ATARI LYNX", "handy"), "jaguar": ("ATARI JAGUAR", "virtualjaguar"),
    "wonderswan": ("WONDERSWAN", "mednafen_wswan"), "ngp": ("NEO GEO POCKET", "mednafen_ngp"),
    "c64": ("COMMODORE 64", "vice_x64"), "amiga": ("AMIGA", "puae"), "msx": ("MSX", "bluemsx"),
    "dos": ("MS-DOS", "dosbox_pure"), "colecovision": ("COLECOVISION", "bluemsx"),
    "intellivision": ("INTELLIVISION", "freeintv"), "3do": ("3DO", "opera"), "virtualboy": ("VIRTUAL BOY", "mednafen_vb"),
}
# disc/image extensions that only make sense once a system folder disambiguates them
_DISC_EXTS = {".iso", ".cue", ".bin", ".chd", ".cso", ".pbp", ".gdi", ".cdi", ".m3u", ".img", ".rom"}
_PLAYABLE_EXTS = set(_ROM_CORES) | _DISC_EXTS


def _system_for(path):
    """(system, core) for a ROM: folder wins (disambiguates disc formats), else extension."""
    p = Path(path)
    if p.suffix.lower() not in _PLAYABLE_EXTS:
        return None
    for part in p.parts[:-1]:                      # any parent folder named after a system
        m = _SYSTEM_FOLDERS.get(part.lower())
        if m:
            return m
    return _ROM_CORES.get(p.suffix.lower())         # else the unambiguous extension map


def games_list():
    roms = []
    try:
        for p in sorted(ROMS_DIR.rglob("*")):
            if not p.is_file():
                continue
            meta = _system_for(p)
            if not meta:
                continue
            url = "/roms/" + quote(p.relative_to(ROMS_DIR).as_posix())
            roms.append({"path": str(p), "url": url, "name": p.stem, "system": meta[0], "core": meta[1]})
            if len(roms) >= 1000:
                break
    except Exception as e:
        log("games_list failed:", e)
    return roms


# Removable-media mount roots to scan for ROMs (Linux/Pi). USB sticks auto-mount
# under these; on Windows these don't exist so import cleanly finds nothing.
_USB_ROOTS = ("/media", "/run/media", "/mnt")


def import_usb_roms():
    """Copy recognizable ROMs off any mounted USB drive into ROMS_DIR.
    Preserves a system subfolder when the stick already uses the roms/<system>/
    convention (so disc formats keep resolving). Returns {ok,count,found,detail}."""
    copied, found, scanned = 0, 0, 0
    try:
        for root in _USB_ROOTS:
            r = Path(root)
            if not r.is_dir():
                continue
            for p in r.rglob("*"):
                scanned += 1
                if scanned > 60000:      # guard against a huge drive
                    break
                try:
                    if not p.is_file() or ROMS_DIR in p.parents:
                        continue
                except OSError:
                    continue
                meta = _system_for(p)
                if not meta:
                    continue
                found += 1
                sysfolder = next((part for part in p.parts[:-1]
                                  if part.lower() in _SYSTEM_FOLDERS), None)
                dest_dir = (ROMS_DIR / sysfolder) if sysfolder else ROMS_DIR
                dest = dest_dir / p.name
                if dest.exists():
                    continue
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest)
                    copied += 1
                except Exception as e:
                    log("usb rom copy failed:", p, e)
                if copied >= 1000:
                    break
    except Exception as e:
        return {"ok": False, "count": copied, "found": found, "detail": str(e)}
    if found == 0:
        return {"ok": False, "count": 0, "found": 0,
                "detail": "no USB drive with recognizable ROMs found (looked in /media, /run/media, /mnt)"}
    detail = ("imported %d new ROM(s)" % copied) if copied else "USB found, but all %d ROM(s) already imported" % found
    return {"ok": True, "count": copied, "found": found, "detail": detail}


async def games_import_usb():
    return await asyncio.get_event_loop().run_in_executor(None, import_usb_roms)


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
        ok = await state.launch_app(msg.get("url"))
        await ws.send(json.dumps({"type": "app.launch", "ok": bool(ok), "url": msg.get("url")}))
    elif t == "app.kill":
        await state.kill_apps()
    elif t == "games.list":
        await ws.send(json.dumps({"type": "games.list", "roms": games_list(), "dir": str(ROMS_DIR)}))
    elif t == "games.import":
        res = await games_import_usb()
        await ws.send(json.dumps({"type": "games.import", **res}))
        await ws.send(json.dumps({"type": "games.list", "roms": games_list(), "dir": str(ROMS_DIR)}))
    elif t == "youtube.search":
        q = (msg.get("query") or "").strip()
        r = await youtube_search(q) if q else {"results": [], "token": None}
        await ws.send(json.dumps({"type": "youtube.results", "query": q,
                                  "results": r["results"], "token": r.get("token")}))
    elif t == "youtube.more":
        token = msg.get("token")
        r = await youtube_more(token) if token else {"results": [], "token": None}
        await ws.send(json.dumps({"type": "youtube.more", "query": msg.get("query"),
                                  "results": r["results"], "token": r.get("token")}))
    elif t == "youtube.random":
        # Mix several DISTINCT seeds per batch and take a few from each, so a
        # single pool isn't 20 videos of the same theme (bailey: "nothing but
        # nature documentaries"). Fetches run concurrently.
        seeds = random.sample(_YT_SEEDS, min(5, len(_YT_SEEDS)))
        batches = await asyncio.gather(*[youtube_search(s) for s in seeds])
        results = []
        for batch in batches:
            vids = batch.get("results", [])
            random.shuffle(vids)
            results.extend(vids[:4])
        random.shuffle(results)
        await ws.send(json.dumps({"type": "youtube.random", "seed": ", ".join(seeds), "results": results}))
    elif t == "wifi.scan":
        await ws.send(json.dumps({"type": "wifi.scan", "networks": await wifi_scan()}))
    elif t == "wifi.join":
        res = await wifi_join(msg.get("ssid"), msg.get("psk"))
        await ws.send(json.dumps({"type": "wifi.join", "ok": res["ok"], "detail": res.get("detail", "")}))
    elif t == "obd.diag":
        result = await state.telemetry.run_diag()
        await ws.send(json.dumps({"type": "obd.diag", "result": result}))
    elif t == "obd.capture":
        result = await state.telemetry.run_capture()
        await ws.send(json.dumps({"type": "obd.capture", "result": result}))
    elif t == "obd.mscan":
        result = await state.telemetry.run_mscan()
        await ws.send(json.dumps({"type": "obd.mscan", "result": result}))
    elif t == "obd.watch":
        result = await state.telemetry.run_watch()
        await ws.send(json.dumps({"type": "obd.watch", "result": result}))
    elif t == "obd.readcodes":
        # Manual, immediate DTC scan (bailey: don't wait 30s for the poll).
        tel = state.telemetry
        if tel.use_raw and tel.raw or tel.conn:
            await tel.poll_dtc()
            await ws.send(json.dumps({"type": "obd.readcodes", "ok": True,
                                      "count": len(tel.dtcs)}))
        else:
            await ws.send(json.dumps({"type": "obd.readcodes", "ok": False,
                                      "detail": "no live OBD link"}))
    elif t == "term.input":
        if not hasattr(state, "term"):
            state.term = TermSession(state.broadcast)
        await state.term.run(str(msg.get("data", "")))
    elif t == "maps.regions":
        await ws.send(json.dumps({"type": "maps.regions",
                                  "regions": list(MAP_REGIONS.keys())}))
    elif t == "maps.download":
        res = await maps_download(state, msg.get("region"),
                                  msg.get("bbox"), msg.get("maxzoom"))
        if res.get("ok"):
            state.broadcast(state.library_payload())   # NAV re-scans maps
        await ws.send(json.dumps({"type": "maps.done", **res}))
    elif t == "bt.scan":
        await ws.send(json.dumps({"type": "bt.scan", "devices": await bt_scan()}))
    elif t == "bt.pair":
        res = await bt_pair(msg.get("mac"), msg.get("name", ""))
        await ws.send(json.dumps({"type": "bt.pair", **res}))
    elif t == "dashcam.set":
        await state.dashcam.set_enabled(bool(msg.get("on")))
    elif t == "power":
        action = msg.get("action")
        if action in ("off", "reboot"):
            cmd = "poweroff" if action == "off" else "reboot"
            try:
                await asyncio.create_subprocess_exec("sudo", "/usr/local/sbin/gost-power", cmd)
            except Exception as e:
                log("power command failed:", e)
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
    elif clean.startswith("/roms/"):
        base, rel = ROMS_DIR, clean[len("/roms/"):]   # served to the in-browser emulator
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
        "Access-Control-Allow-Origin: *",   # lets the in-browser emulator fetch /roms cross-origin (dev)
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
    import traceback
    if websockets is None:
        log("FATAL: 'websockets' package not installed (pip install websockets)")
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        # Full traceback to the journal so a startup crash is diagnosable
        # instead of a silent restart loop. systemd Restart=always with a
        # paced RestartSec then retries without hammering.
        log("FATAL: backend crashed with traceback:")
        traceback.print_exc()
        sys.exit(1)
