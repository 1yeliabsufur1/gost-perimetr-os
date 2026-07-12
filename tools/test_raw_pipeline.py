#!/usr/bin/env python3
"""Offline regression test for the raw ELM327 reader.

Replays an OBDLink EX talking to the F-150 PowerBoost in the exact format the
real adapter uses with our init (echo off, headers off, spaces on), INCLUDING
multi-ECU responses (three ECUs answer 010C, as the truck's own debug log
showed). Proves the whole read path -- RawOBD init, protocol force, query,
tokenize, decode -- produces correct LIVE values without any hardware.

Guards against the 2026-07-11 regression where ATS0 (spaces off) made every
read silently return None.

    python3 tools/test_raw_pipeline.py
"""
import importlib.util
import sys
import types


class MockSerial:
    """Canned OBDLink EX + PowerBoost responses (headers off, spaces on)."""
    def __init__(self, port, baud, timeout=1):
        self.buf = b""

    def write(self, data):
        cmd = data.decode(errors="ignore").strip().upper()
        # Strip the ELM327 expected-response-count hint ('010C 1' -> '010C')
        # that query_pid appends for speed.
        if cmd and not cmd.startswith("AT"):
            cmd = cmd.split()[0]
        if cmd == "" or cmd.startswith("AT"):
            self.buf = b"OK\r\r>"
        elif cmd == "0100":
            self.buf = b"41 00 BE 3F A8 13\r\r>"
        elif cmd == "010C":                       # RPM, 3 ECUs, 1726 rpm
            self.buf = b"41 0C 1A F8\r41 0C 1A F8\r41 0C 1A F8\r\r>"
        elif cmd == "010D":                       # speed 35 km/h
            self.buf = b"41 0D 23\r\r>"
        elif cmd == "0105":                       # coolant 90 C
            self.buf = b"41 05 82\r\r>"
        elif cmd == "03":                         # one stored DTC: P0143
            self.buf = b"43 01 01 43 00 00\r\r>"
        else:
            self.buf = b"NO DATA\r\r>"

    @property
    def in_waiting(self):
        return len(self.buf)

    def read(self, n):
        d, self.buf = self.buf[:n], self.buf[n:]
        return d

    def reset_input_buffer(self):
        self.buf = b""

    def close(self):
        pass


def main():
    ser = types.ModuleType("serial")
    ser.Serial = MockSerial
    sys.modules["serial"] = ser
    sys.modules.setdefault("websockets", types.ModuleType("websockets"))

    root = __file__.rsplit("tools", 1)[0]
    spec = importlib.util.spec_from_file_location("hud", root + "backend/hud_server.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)

    raw = h.RawOBD("/dev/ttyUSB0", 115200, "6")
    rpm, spd, cool, dtcs = raw.read("RPM"), raw.read("SPEED"), raw.read("COOLANT_TEMP"), raw.read_dtcs()
    raw.close()
    assert rpm == 1726.0, f"RPM {rpm}"
    assert spd == 35.0, f"SPEED {spd}"
    assert cool == 90.0, f"COOLANT {cool}"
    assert dtcs and dtcs[0][0] == "P0143", f"DTC {dtcs}"

    linked = h._try_raw("/dev/ttyUSB0", "6", 115200)
    assert linked is not None and linked.read("RPM") == 1726.0
    linked.close()
    print("PASS: raw pipeline decodes multi-ECU truck responses (RPM 1726, "
          "SPEED 35, COOLANT 90, DTC P0143); _try_raw links.")


if __name__ == "__main__":
    main()
