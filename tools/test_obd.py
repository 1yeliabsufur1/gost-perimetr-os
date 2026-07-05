#!/usr/bin/env python3
"""GOST OBD2 diagnostic -- run this on the Pi to see exactly how your adapter
and truck respond, independent of the backend.

    /opt/gost/venv/bin/python3 /opt/gost/tools/test_obd.py

It scans every serial port and baud rate the backend tries, and on the first
working combo dumps the live RPM/speed/coolant + stored trouble codes.
"""
import glob
import sys

try:
    import obd
except ImportError:
    print("python-obd not installed. Use the project venv:")
    print("  /opt/gost/venv/bin/python3 /opt/gost/tools/test_obd.py")
    sys.exit(1)

BAUDS = [115200, 230400, 38400, 9600]

print("=== GOST OBD2 Diagnostic ===")
ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
print("Serial ports found:", ports or "NONE")
if not ports:
    print("\nNo adapter. Check: it's plugged into USB, the truck key is ON,")
    print("and `ls -l /dev/ttyUSB* /dev/ttyACM*` shows a device.")
    sys.exit(1)

conn = None
for port in ports:
    for baud in BAUDS:
        print(f"\nTrying {port} @ {baud} baud ...", end=" ", flush=True)
        try:
            c = obd.OBD(port, baudrate=baud, timeout=2, fast=False)
        except Exception as e:
            print("error:", e)
            continue
        if c.is_connected():
            print("CONNECTED")
            conn = c
            break
        print("no ECU response")
        try:
            c.close()
        except Exception:
            pass
    if conn:
        break

if not conn:
    print("\nAdapter seen but no vehicle link on any port/baud.")
    print("Make sure the ignition is ON (engine running is best), then retry.")
    sys.exit(2)

print("\n=== LINK ESTABLISHED ===")
try:
    print("Protocol :", conn.protocol_name())
except Exception:
    pass
sup = sorted(c.name for c in conn.supported_commands)
print(f"Supported PIDs ({len(sup)}):")
print("  " + ", ".join(sup))

print("\n=== LIVE VALUES ===")
for name in ("RPM", "SPEED", "COOLANT_TEMP", "FUEL_LEVEL",
             "CONTROL_MODULE_VOLTAGE", "INTAKE_PRESSURE"):
    cmd = getattr(obd.commands, name, None)
    if cmd is None:
        continue
    try:
        r = conn.query(cmd)
        print(f"  {name:24s}: {r.value if r and not r.is_null() else '(no answer)'}")
    except Exception as e:
        print(f"  {name:24s}: error {e}")

print("\n=== STORED TROUBLE CODES (DTC) ===")
try:
    r = conn.query(obd.commands.GET_DTC)
    codes = r.value if r and not r.is_null() else []
    if codes:
        for code, desc in codes:
            print(f"  {code}: {desc}")
    else:
        print("  none")
except Exception as e:
    print("  DTC query error:", e)

conn.close()
print("\nDone. If you see live RPM/SPEED above, the truck link works and the")
print("dashboard's AUTO mode will show LIVE data on this same adapter.")
