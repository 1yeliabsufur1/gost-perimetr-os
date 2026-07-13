#!/usr/bin/env python3
"""gost-obd-probe -- capture Ford-specific OBD data so doors / TPMS / oil-life
can be mapped to PIDs. Writes logs to the FAT boot partition so you can pull
them off the SD card from a PC (no SSH, no scp).

Run from the TERM tab (or SSH):

  sudo gost-obd-probe info
      Show the adapter, port, and where logs are written.

  sudo gost-obd-probe sweep
      Query a curated set of Ford mode-22 body PIDs (door/TPMS/oil-life
      candidates) plus a modest range, log every non-empty response WITH its
      ECU header. One-shot; safe with the engine on or off.

  sudo gost-obd-probe capture <label> <seconds> [init]
      Monitor ALL bus traffic (headers on) for <seconds>, timestamped. Do an
      action during the window. Run it twice to diff, e.g.:
          sudo gost-obd-probe capture doors-closed 15
          # ... open the driver door ...
          sudo gost-obd-probe capture driver-door-open 15

      [init] is an OPTIONAL semicolon-separated list of AT/ST commands sent
      before monitoring, so you can pick the bus WITHOUT reflashing. Ford
      doors are usually on MS-CAN, not the default HS-CAN. Both OBDLink EX and
      MX+ (STN chips) can switch. Try, in order, whichever gives changing
      bytes when a door opens:
          # default = HS-CAN (500k), no init needed
          sudo gost-obd-probe capture door-open-hs 15
          # Ford MS-CAN (125k) via the STN user protocol:
          sudo gost-obd-probe capture door-open-ms 15 "STP53;ATH1;ATMA"
      The log records each init command's reply, so we can see if the bus
      switch took ('OK' vs '?') -- send it back either way.

Then connect the SD card to a PC and copy the logs from the boot drive:
  <BOOT>/gost-obd/*.log     (the small FAT drive Windows shows on insert)

Send those .log files back and the changed bytes get mapped to the feature.

Notes:
- The backend OBD service holds the port; this tool stops it for the session
  and restarts it after, so they don't fight over the adapter.
- Doors are often on Ford MS-CAN; if a plain capture shows nothing changing,
  say so -- the MX+ can switch buses and we'll add that pass.
"""
import glob
import os
import subprocess
import sys
import time

BAUDS = [115200, 230400, 38400, 9600]

# Curated Ford mode-22 DIDs worth trying first (body/chassis), then a modest
# sweep fills gaps. Not authoritative -- the capture-diff is the real source.
FORD_DIDS = [
    "2000", "2001", "2002",              # door/latch status blocks (candidates)
    "402A", "402B",                       # TPMS pressure blocks (candidates)
    "4028", "4029",
    "1E12",                               # oil life (candidate)
    "DD00", "DD01", "DD04", "DD05",       # generic DID range Fords populate
]


def boot_log_dir():
    for base in ("/boot/firmware", "/boot"):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            d = os.path.join(base, "gost-obd")
            try:
                os.makedirs(d, exist_ok=True)
                return d
            except OSError:
                continue
    d = os.path.expanduser("~/gost-obd")
    os.makedirs(d, exist_ok=True)
    return d


def find_port():
    ports = sorted(glob.glob("/dev/rfcomm*")) + \
        sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return ports[0] if ports else None


def open_serial(port):
    import serial
    for baud in BAUDS:
        try:
            s = serial.Serial(port, baud, timeout=1)
            s.write(b"\r")
            time.sleep(0.3)
            s.reset_input_buffer()
            s.write(b"ATZ\r")
            time.sleep(1.2)
            resp = _read(s, 2.0)
            if "ELM" in resp.upper() or "OK" in resp.upper() or resp.strip():
                return s, baud
            s.close()
        except Exception:
            pass
    return None, None


def _read(s, secs):
    buf = b""
    end = time.time() + secs
    while time.time() < end:
        n = s.in_waiting
        if n:
            buf += s.read(n)
            if b">" in buf:
                break
        else:
            time.sleep(0.01)
    return buf.decode(errors="replace")


def cmd(s, text, wait=0.3, read=2.0):
    s.reset_input_buffer()
    s.write((text + "\r").encode())
    if wait:
        time.sleep(wait)
    return _read(s, read)


def init(s, headers=True):
    for c in ("ATE0", "ATL0", "ATS1", "ATH" + ("1" if headers else "0"), "ATSP6"):
        cmd(s, c, 0.2)
    cmd(s, "0100", 0.3, 6.0)   # wake the bus / trigger protocol search


def stop_backend():
    for unit in ("hud-backend", "obd-rfcomm"):
        subprocess.run(["systemctl", "stop", unit],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_backend():
    for unit in ("obd-rfcomm", "hud-backend"):
        subprocess.run(["systemctl", "start", unit],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def do_info():
    port = find_port()
    print("PORT:", port or "NONE FOUND (plug in / pair the adapter)")
    print("LOG DIR:", boot_log_dir())
    print("  -> pull *.log off the FAT boot drive from a PC")


def do_sweep():
    port = find_port()
    if not port:
        print("no adapter found"); return
    stop_backend(); time.sleep(1.0)
    try:
        s, baud = open_serial(port)
        if not s:
            print("could not open", port); return
        init(s, headers=True)
        path = os.path.join(boot_log_dir(), "sweep-%d.log" % int(time.time()))
        with open(path, "w") as f:
            f.write("# gost-obd-probe sweep  port=%s baud=%s  %s\n"
                    % (port, baud, time.strftime("%Y-%m-%d %H:%M:%S")))
            dids = list(FORD_DIDS) + ["%04X" % n for n in range(0x2000, 0x2040)]
            for did in dids:
                r = cmd(s, "22" + did, 0.0, 1.2).replace("\r", " ").strip()
                up = r.upper()
                if r and "NO DATA" not in up and "ERROR" not in up and "?" not in r:
                    line = "22%s -> %s" % (did, r)
                    f.write(line + "\n"); print(line)
        s.close()
        print("\nWROTE:", path)
    finally:
        start_backend()


def do_capture(label, secs, init_cmds=None):
    port = find_port()
    if not port:
        print("no adapter found"); return
    stop_backend(); time.sleep(1.0)
    try:
        s, baud = open_serial(port)
        if not s:
            print("could not open", port); return
        safe = "".join(c for c in label if c.isalnum() or c in "-_") or "capture"
        path = os.path.join(boot_log_dir(),
                            "%s-%d.log" % (safe, int(time.time())))
        f = open(path, "w")
        f.write("# gost-obd-probe capture '%s'  port=%s baud=%s  %s\n"
                % (label, port, baud, time.strftime("%Y-%m-%d %H:%M:%S")))
        already_monitoring = False
        if init_cmds:
            # Custom bus/init sequence (e.g. MS-CAN). Log each reply so we can
            # see whether the switch took ('OK' vs '?').
            f.write("# custom init: %s\n" % init_cmds)
            for c in [x.strip() for x in init_cmds.split(";") if x.strip()]:
                r = cmd(s, c, 0.2, 3.0).replace("\r", " ").strip()
                f.write("# %s -> %s\n" % (c, r))
                if c.upper().replace(" ", "") in ("ATMA", "STMA"):
                    already_monitoring = True
        else:
            init(s, headers=True)
        print("CAPTURING %ds -> do the action now..." % secs)
        if not already_monitoring:
            s.reset_input_buffer()
            s.write(b"ATMA\r")   # monitor all frames
        t0 = time.time()
        while time.time() - t0 < secs:
            n = s.in_waiting
            if n:
                chunk = s.read(n).decode(errors="replace")
                for ln in chunk.replace("\r", "\n").split("\n"):
                    ln = ln.strip()
                    if ln and ln != ">":
                        f.write("%.3f %s\n" % (time.time() - t0, ln))
            else:
                time.sleep(0.005)
        f.close()
        cmd(s, "", 0.1)   # any char stops ATMA
        s.close()
        print("WROTE:", path)
    finally:
        start_backend()


def main():
    if os.geteuid() != 0:
        print("run with sudo"); sys.exit(1)
    a = sys.argv[1:]
    if not a or a[0] == "info":
        do_info()
    elif a[0] == "sweep":
        do_sweep()
    elif a[0] == "capture" and len(a) >= 3:
        do_capture(a[1], max(3, min(120, int(a[2]))),
                   a[3] if len(a) >= 4 else None)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
