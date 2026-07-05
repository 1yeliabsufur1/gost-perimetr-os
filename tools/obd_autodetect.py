#!/usr/bin/env python3
"""One-shot OBD auto-detect + PERSIST, using the SAME raw ELM327 reader the
backend uses (not python-obd, which desyncs on some trucks). Finds the
protocol that actually reads your vehicle, PROVES it (prints live RPM/speed/
coolant + any stored trouble codes), and saves state/obd.json so the backend
links on the first try from then on.

Run with the backend STOPPED (they can't share the serial port):

    sudo systemctl stop hud-backend
    sudo /opt/gost/venv/bin/python3 /opt/gost/tools/obd_autodetect.py
    sudo systemctl start hud-backend
"""
import glob
import json
import os
import sys
import time

# Reuse the backend's RawOBD/_reset_adapter so this tests the EXACT code path
# that runs in production.
HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("/opt/gost/backend", os.path.join(HERE, "..", "backend")):
    if os.path.isdir(p):
        sys.path.insert(0, p)
try:
    from hud_server import RawOBD, _reset_adapter, RAW_PIDS  # noqa
except Exception as e:
    print("could not import backend RawOBD:", e)
    print("run with: /opt/gost/venv/bin/python3 /opt/gost/tools/obd_autodetect.py")
    sys.exit(1)

STATE = os.environ.get("GOST_STATE", "/opt/gost/state")


def main():
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    print("=== GOST OBD auto-detect (raw ELM327) ===")
    print("Ports:", ports or "NONE")
    if not ports:
        print("No adapter. Plug the OBDLink in, key in RUN, and retry.")
        return 1

    for port in ports:
        for proto in ("6", "7", "8", None):
            tag = proto or "auto"
            print(f"\n-> {port}  protocol={tag}", flush=True)
            _reset_adapter(port, 115200)
            try:
                raw = RawOBD(port, 115200, proto)
            except Exception as e:
                print("   open error:", e)
                continue
            rpm = raw.read("RPM")
            print("   forced RPM =", rpm)
            if rpm is not None:
                spd = raw.read("SPEED")
                cool = raw.read("COOLANT_TEMP")
                dtcs = []
                try:
                    dtcs = raw.read_dtcs()
                except Exception:
                    pass
                raw.close()
                os.makedirs(STATE, exist_ok=True)
                with open(os.path.join(STATE, "obd.json"), "w") as f:
                    json.dump({"port": port, "baud": 115200,
                               "protocol": proto, "raw": True}, f)
                try:
                    import pwd
                    u = pwd.getpwnam(open("/etc/gost-user").read().strip())
                    os.chown(os.path.join(STATE, "obd.json"), u.pw_uid, u.pw_gid)
                except Exception:
                    pass
                print(f"\n*** SUCCESS: {port} protocol {tag} ***")
                print(f"*** RPM={rpm}  SPEED={spd} km/h  COOLANT={cool} C ***")
                print(f"*** DTCs: {[c for c, _ in dtcs] or 'none'} ***")
                print(f"*** Saved {STATE}/obd.json -- start the backend; DRIVE shows LIVE. ***")
                return 0
            raw.close()
    print("\n*** No protocol read the vehicle. Copy ALL output to Claude. ***")
    print("*** Verify: key in RUN (engine on), adapter LED on, ls /dev/ttyUSB* ***")
    return 2


if __name__ == "__main__":
    sys.exit(main())
