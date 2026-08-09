"""Minimal ELM327 reader for GOST MINI over a Bluetooth rfcomm serial port.

Deliberately dependency-light (pyserial only) and cut down to what a slow
e-paper display actually needs: trouble codes + a handful of slow PIDs.
Mirrors the protocol handling proven in the main GOST backend.
"""
import glob
import os
import time

try:
    import serial
except ImportError:                      # lets the simulator run off-Pi
    serial = None

# name -> (request, decoder over the data bytes after the "41 xx" header)
PIDS = {
    "volts":     ("0142", lambda b: (256 * b[0] + b[1]) / 1000.0 if len(b) >= 2 else None),
    "coolant_c": ("0105", lambda b: float(b[0] - 40) if b else None),
    "fuel":      ("012F", lambda b: b[0] * 100.0 / 255 if b else None),
    "soc":       ("015B", lambda b: b[0] * 100.0 / 255 if b else None),   # hybrid/EV pack
    "speed":     ("010D", lambda b: float(b[0]) if b else None),
    "maf":       ("0110", lambda b: (256 * b[0] + b[1]) / 100.0 if len(b) >= 2 else None),
}


def find_port():
    """A paired BT OBD adapter shows up as /dev/rfcomm*; USB as ttyUSB/ttyACM."""
    for pat in ("/dev/rfcomm*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


class MiniOBD:
    def __init__(self, port=None, baud=38400):
        self.port = port or find_port()
        self.baud = baud
        self.ser = None
        self.detail = ""

    def connect(self):
        """Open the adapter and PROVE it answers. Just opening /dev/rfcomm0
        isn't enough: after the truck is switched off the node still exists but
        the far end is gone, so a bare open() can 'succeed' against a dead link.
        We require a real reply before calling it connected."""
        if serial is None:
            self.detail = "pyserial not installed"
            return False
        self.port = self.port or find_port()
        if not self.port:
            self.detail = "no rfcomm device; is the adapter paired?"
            self._rebind()                    # maybe the bind was lost
            return False
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
            time.sleep(0.4)
            for cmd in ("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0"):
                self._cmd(cmd, 0.3)
            probe = self._cmd("0100", 0.6, 6.0)    # forces protocol search
            up = probe.upper()
            if not probe.strip() or ("41 00" not in up and "4100" not in up.replace(" ", "")):
                # Opened, but nothing answered. Nearly always the key is off, so
                # the adapter has no power -- re-binding can't fix that and just
                # churns the link. Only rebind after several failures in a row,
                # which is the case that actually indicates a STALE binding.
                self.detail = "adapter not answering (key on?)"
                self.close()
                self._silent = getattr(self, "_silent", 0) + 1
                if self._silent >= 6:
                    self._silent = 0
                    self._rebind()
                return False
            self._silent = 0
            self.detail = "linked on " + self.port
            return True
        except Exception as e:
            msg = str(e)
            self.detail = ("permission denied on %s -- add your user to 'dialout'" % self.port) \
                if "Permission" in msg or "denied" in msg.lower() else msg[:60]
            self.ser = None
            if "Permission" not in msg:
                self._rebind()          # a real open() error DOES suggest a stale bind
            return False

    def _rebind(self):
        """Nudge the supervised link service to re-establish the connection.

        gost-obd-link.service holds the link with `rfcomm connect` and systemd
        reconnects it automatically, so this is only a fallback for the case
        where that service isn't installed (e.g. a manual `rfcomm bind` setup).
        Rate-limited to once every 2 minutes, best-effort."""
        mac = self._saved_mac()
        if not mac:
            return
        now = time.time()
        if now - getattr(self, "_last_rebind", 0) < 120:
            return
        self._last_rebind = now
        import subprocess
        try:
            r = subprocess.run(["systemctl", "is-enabled", "gost-obd-link.service"],
                               capture_output=True, text=True, timeout=5)
            if "enabled" in r.stdout:
                subprocess.run(["sudo", "-n", "systemctl", "restart", "gost-obd-link.service"],
                               capture_output=True, timeout=15)
                time.sleep(2)
                self.port = find_port()
                return
        except Exception:
            pass
        for args in (["sudo", "-n", "rfcomm", "release", "0"],
                     ["sudo", "-n", "rfcomm", "bind", "0", mac]):
            try:
                subprocess.run(args, capture_output=True, timeout=10)
            except Exception:
                return
        time.sleep(1.0)
        self.port = find_port()

    @staticmethod
    def _saved_mac():
        """MAC written by pair-obd.sh so we can re-bind unattended."""
        for p in (os.path.expanduser("~/.gost-obd-mac"), "/etc/gost-obd-mac"):
            try:
                with open(p) as f:
                    mac = f.read().strip()
                if len(mac) == 17 and mac.count(":") == 5:
                    return mac
            except Exception:
                continue
        return None

    def alive(self):
        return self.ser is not None and self.ser.is_open

    def close(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def _cmd(self, s, wait=0.25, max_read=3.0):
        if not self.ser:
            return ""
        try:
            self.ser.reset_input_buffer()
            self.ser.write((s + "\r").encode())
            time.sleep(wait)
            out, t0 = b"", time.time()
            while time.time() - t0 < max_read:
                chunk = self.ser.read(self.ser.in_waiting or 1)
                if chunk:
                    out += chunk
                    if b">" in out:
                        break
                elif out:
                    break
            return out.decode(errors="ignore")
        except Exception:
            self.close()
            return ""

    @staticmethod
    def _toks(line):
        return [t for t in line.replace("\r", " ").replace("\n", " ").split(" ")
                if len(t) == 2 and all(c in "0123456789ABCDEFabcdef" for c in t)]

    def read_pid(self, name):
        spec = PIDS.get(name)
        if not spec:
            return None
        req, decode = spec
        resp = self._cmd(req, 0.12, 1.6).upper()
        if "NO DATA" in resp or "UNABLE" in resp or "ERROR" in resp:
            return None
        toks = self._toks(resp)
        want = "4" + req[1]                       # 01xx -> 41
        for i in range(len(toks) - 1):
            if toks[i] == want and toks[i + 1] == req[2:4]:
                try:
                    return decode([int(t, 16) for t in toks[i + 2:]])
                except Exception:
                    return None
        return None

    def read_vitals(self):
        """The slow set that suits e-paper. Missing PIDs come back as None."""
        v = {}
        for k in ("volts", "coolant_c", "fuel", "soc"):
            v[k] = self.read_pid(k)
        c = v.get("coolant_c")
        v["coolant_f"] = (c * 9 / 5 + 32) if c is not None else None
        return v

    @staticmethod
    def _decode_dtc(a, b):
        letter = "PCBU"[(a & 0xC0) >> 6]
        return "%s%d%X%X%X" % (letter, (a & 0x30) >> 4, a & 0x0F, (b & 0xF0) >> 4, b & 0x0F)

    def read_codes(self):
        """Modes 03/07/0A -> [(code, kind)] across every responding ECU."""
        seen = {}
        for mode, reply, kind in (("03", "43", "stored"), ("07", "47", "pending"), ("0A", "4A", "permanent")):
            resp = self._cmd(mode, 0.4, 3.0).upper()
            if "NO DATA" in resp or "ERROR" in resp or "UNABLE" in resp:
                continue
            for line in resp.replace("\r", "\n").split("\n"):
                toks = self._toks(line)
                if reply not in toks:
                    continue
                rest = toks[toks.index(reply) + 1:]
                if len(rest) % 2 == 1:            # leading DTC-count byte
                    rest = rest[1:]
                for i in range(0, len(rest) - 1, 2):
                    a, b = int(rest[i], 16), int(rest[i + 1], 16)
                    if a or b:
                        seen.setdefault(self._decode_dtc(a, b), kind)
        return [(c, k) for c, k in seen.items()]

    def clear_codes(self):
        r = self._cmd("04", 0.6, 3.0).upper()
        return "OK" in r or "44" in r
