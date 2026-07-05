#!/usr/bin/env python3
"""One-shot OBD auto-detect + PERSIST. Finds the protocol/baud that actually
reads your truck, PROVES it (prints live RPM), and saves it to
state/obd.json so the backend links on the first try from then on.

Run it with the backend STOPPED (they can't share the serial port):

    sudo systemctl stop hud-backend
    sudo /opt/gost/venv/bin/python3 /opt/gost/tools/obd_autodetect.py
    sudo systemctl start hud-backend

On success it prints "*** SUCCESS ... Live RPM = N ***" and you're done --
start the backend and DRIVE shows LIVE. On failure it prints everything for
Claude.
"""
import glob
import json
import logging
import os
import sys
import time

try:
    import obd
    import serial
except ImportError as e:
    print("missing dep:", e, "-- run with /opt/gost/venv/bin/python3")
    sys.exit(1)

logging.getLogger("obd").setLevel(logging.WARNING)
STATE = os.environ.get("GOST_STATE", "/opt/gost/state")


def drain(port, baud):
    """ATZ + drain-to-silence so python-obd starts from a clean buffer."""
    try:
        s = serial.Serial(port, baud, timeout=0.4)
        s.write(b"\r"); time.sleep(0.2); s.reset_input_buffer()
        s.write(b"ATZ\r"); time.sleep(1.5)
        end = time.time() + 1.5
        while time.time() < end:
            if s.in_waiting:
                s.read(s.in_waiting); time.sleep(0.1)
            else:
                time.sleep(0.15)
                if not s.in_waiting:
                    break
        s.reset_input_buffer(); s.close(); time.sleep(0.4)
    except Exception as e:
        print("  drain error:", e)


def main():
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    print("=== GOST OBD auto-detect ===")
    print("Ports:", ports or "NONE")
    if not ports:
        print("No adapter found. Plug the OBDLink in, key in RUN, and retry.")
        return 1

    combos = [("6", 115200), ("7", 115200), ("8", 115200),
              (None, 115200), (None, 38400)]
    for port in ports:
        for proto, baud in combos:
            tag = proto or "auto"
            print(f"\n-> {port}  protocol={tag}  @ {baud}", flush=True)
            drain(port, baud)
            try:
                c = obd.OBD(port, baudrate=baud, protocol=proto,
                            fast=False, timeout=12, check_voltage=False)
            except Exception as e:
                print("   connect error:", e)
                continue
            try:
                print("   status:", c.status(), "| proto:", c.protocol_name())
            except Exception:
                pass
            rpm = None
            try:
                r = c.query(obd.commands.RPM, force=True)
                rpm = r.value if (r and not r.is_null()) else None
            except Exception as e:
                print("   query error:", e)
            print("   forced RPM =", rpm)
            if rpm is not None:
                os.makedirs(STATE, exist_ok=True)
                cfg = {"port": port, "baud": baud, "protocol": proto}
                with open(os.path.join(STATE, "obd.json"), "w") as f:
                    json.dump(cfg, f)
                spd = None
                try:
                    sr = c.query(obd.commands.SPEED, force=True)
                    spd = sr.value if (sr and not sr.is_null()) else None
                except Exception:
                    pass
                print(f"\n*** SUCCESS: {port} protocol {tag} @ {baud} ***")
                print(f"*** Live RPM = {rpm}   SPEED = {spd} ***")
                print(f"*** Saved to {STATE}/obd.json -- start the backend and")
                print("*** DRIVE will show LIVE - OBDLink. Done. ***")
                try:
                    c.close()
                except Exception:
                    pass
                # chown so the gost-user backend can read it
                try:
                    import pwd
                    u = pwd.getpwnam(open("/etc/gost-user").read().strip())
                    os.chown(os.path.join(STATE, "obd.json"), u.pw_uid, u.pw_gid)
                except Exception:
                    pass
                return 0
            try:
                c.close()
            except Exception:
                pass
    print("\n*** No working protocol found. Copy ALL output above to Claude. ***")
    print("*** Check: key in RUN (engine on), adapter LED on, `ls /dev/ttyUSB*` ***")
    return 2


if __name__ == "__main__":
    sys.exit(main())
