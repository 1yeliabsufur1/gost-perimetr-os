"""Door decoding tests -- run with:  python tests/test_door_frame.py

These pin down the 0x3B3 "BodyInfo_3_FD1" bit map that door status is read
from. The map came from Ford's own CAN database (comma.ai/opendbc,
ford_lincoln_base_pt.dbc, message 947) rather than from guesswork at the
truck, so it's worth a test that fails loudly if someone "tidies" the masks:
getting a bit wrong here means the dash cheerfully reports the wrong door, or
worse, reports SECURE with something open.

No hardware needed -- the parser and the bit maths are pure functions.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from hud_server import RawOBD  # noqa: E402

BITS = dict((n, (i, m)) for n, i, m in RawOBD.DOOR_FRAME_BITS)

# name -> DBC start bit, straight out of the .dbc SG_ lines
DBC_BITS = {"trunk": 0, "rl": 48, "rr": 49, "inner_tailgate": 58,
            "hood": 59, "fr": 60, "fl": 61}

fails = []


def check(label, got, want):
    ok = got == want
    print("  %-46s %s" % (label, "ok" if ok else
                          "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        fails.append(label)


def payload(**ajar):
    """8-byte body with the named openings ajar (DBC: 1 = Ajar)."""
    data = [0] * 8
    for name in ajar:
        i, m = BITS[name]
        data[i] |= m
    return data


def elm(data):
    """Rendered the way an ELM327 emits it in ATMA monitor mode."""
    return ("3B3 " + " ".join("%02X" % b for b in data) + "\r").encode()


print("1. byte/mask map still matches the DBC start bits")
for name, bit in DBC_BITS.items():
    check("%s (DBC bit %d)" % (name, bit), BITS[name],
          (bit // 8, 1 << (bit % 8)))
check("no opening is missing", sorted(BITS), sorted(DBC_BITS))

print("2. all closed reads as all closed")
check("nothing ajar", RawOBD.decode_doors(payload()),
      {n: False for n in BITS})

print("3. each opening alone sets exactly its own bit")
for name in BITS:
    state = RawOBD.decode_doors(payload(**{name: True}))
    check(name + " alone", [k for k, v in state.items() if v], [name])

print("4. openings sharing a byte don't bleed into each other")
data = payload(fl=True, hood=True)          # both live in byte 7
check("byte 7 = 0x28", data[7], 0x28)
check("exactly those two", sorted(k for k, v in
                                  RawOBD.decode_doors(data).items() if v),
      ["fl", "hood"])
data = payload(rl=True, rr=True)            # both live in byte 6
check("byte 6 = 0x03", data[6], 0x03)

print("5. parsing the monitor stream")
DID = RawOBD.DOOR_FRAME_ID
check("single frame", RawOBD._frame_bytes(elm(payload(fl=True)), DID),
      payload(fl=True))
check("latest frame wins",
      RawOBD._frame_bytes(elm(payload()) + elm(payload(rl=True)), DID),
      payload(rl=True))
check("truncated trailing line ignored",
      RawOBD._frame_bytes(elm(payload(hood=True)) + b"3B3 00 00 00", DID),
      payload(hood=True))
check("other arbitration IDs ignored",
      RawOBD._frame_bytes(elm(payload(trunk=True)) + b"040 11 22 33\r", DID),
      payload(trunk=True))
# The TPMS frame sits one ID along and would otherwise look identical.
check("neighbouring 3B5 not mistaken for 3B3",
      RawOBD._frame_bytes(b"3B5 00 F0 00 F0 00 F0 00 F0\r", DID), None)
check("no frame -> None", RawOBD._frame_bytes(b"NO DATA\r>", DID), None)
check("empty -> None", RawOBD._frame_bytes(b"", DID), None)

print("6. frames split across serial reads still decode")
# The watcher reads whatever bytes happen to be waiting, so frames routinely
# straddle a read boundary. Replay one byte at a time -- the worst case.
stream = elm(payload()) + elm(payload(fr=True))
buf, got = b"", None
for i in range(len(stream)):
    buf = (buf + stream[i:i + 1])[-512:]
    d = RawOBD._frame_bytes(buf, DID)
    if d is not None:
        got = d
        buf = buf[buf.rfind(b"\r") + 1:] if b"\r" in buf else b""
check("byte-at-a-time reassembly", got, payload(fr=True))

print("7. TPMS (0x3B5) decode")
# DBC: four 16-bit big-endian kilopascal values, LF/RF/RR/LR.
TP_DBC = {"fl": 7, "fr": 23, "rr": 39, "rl": 55}
for name, i in RawOBD.TPMS_BYTES:
    # a 16-bit Motorola signal starting at bit N begins in byte N // 8
    check("%s starts at byte %d (DBC bit %d)" % (name, i, TP_DBC[name]),
          i, TP_DBC[name] // 8)


def kpa_frame(fl, fr, rr, rl):
    out = []
    for v in (fl, fr, rr, rl):        # note the DBC's LF, RF, RR, LR order
        out += [(v >> 8) & 0xFF, v & 0xFF]
    return out


# 241 kPa = 35.0 psi, 221 = 32.0, 262 = 38.0
check("typical pressures -> psi",
      RawOBD.decode_tpms(kpa_frame(241, 241, 221, 262)),
      {"fl": 35.0, "fr": 35.0, "rr": 32.1, "rl": 38.0})
check("corners don't swap",
      RawOBD.decode_tpms(kpa_frame(100, 200, 300, 400)),
      {"fl": 14.5, "fr": 29.0, "rr": 43.5, "rl": 58.0})
check("65533 sentinel -> None, not a 9500 psi tyre",
      RawOBD.decode_tpms(kpa_frame(241, 65533, 241, 241))["fr"], None)
check("all sentinels -> no reading at all",
      RawOBD.decode_tpms(kpa_frame(65533, 65533, 65533, 65533)), None)
check("absurd value rejected",
      RawOBD.decode_tpms(kpa_frame(241, 5000, 241, 241))["fr"], None)
check("flat tyre still reported",
      RawOBD.decode_tpms(kpa_frame(241, 241, 241, 138))["rl"], 20.0)

print("8. the non-door half of 0x3B3 (ignition / lamps / dash dimmer)")


def body(b0=0, b1=0, b3=0, b6=0):
    d = [0] * 8
    d[0], d[1], d[3], d[6] = b0, b1, b3, b6
    return RawOBD.decode_body(d)


# Ignition_Status is byte 0 bits 7-4: 1 off, 2 accessory, 4 run, 8 start.
for raw, want in [(1, "off"), (2, "accessory"), (4, "run"), (8, "start"),
                  (15, "invalid"), (0, "unknown")]:
    check("ignition %d -> %s" % (raw, want), body(b0=raw << 4)["ignition"], want)
# Byte 0 also holds the tailgate (bit 0) and parklamps (bits 3-2). Ignition
# must not bleed into them or vice versa.
check("ignition ignores the tailgate bit", body(b0=(4 << 4) | 0x01)["ignition"], "run")
check("parklamps on", body(b0=1 << 2)["parklamps"], True)
check("parklamps off", body(b0=0)["parklamps"], False)
check("parklamps ignore ignition", body(b0=(4 << 4) | (1 << 2))["parklamps"], True)

# Day_Night_Status is byte 1 bits 7-6: 1 Day, 2 Night, 0/3 no opinion.
check("day", body(b1=1 << 6)["night"], False)
check("night", body(b1=2 << 6)["night"], True)
check("null -> unknown", body(b1=0)["night"], None)
check("notused -> unknown", body(b1=3 << 6)["night"], None)
check("key in ignition", body(b1=1 << 3)["key_in"], True)
check("key_in ignores day/night", body(b1=(2 << 6) | (1 << 3))["key_in"], True)
check("night ignores key_in", body(b1=(2 << 6) | (1 << 3))["night"], True)

# Dimming_Lvl is all of byte 3: 0 off, 1-12 night steps, 13-18 day steps.
check("dimmer off -> 0.0", body(b3=0)["dim"], 0.0)
check("Night_1 -> dimmest", body(b3=1)["dim"], 1 / 12.0)
check("Night_12 -> full", body(b3=12)["dim"], 1.0)
check("Day_1 -> full", body(b3=13)["dim"], 1.0)
check("Day_6 -> full", body(b3=18)["dim"], 1.0)
check("unknown (254) -> None, don't blank the screen", body(b3=254)["dim"], None)
check("invalid (255) -> None", body(b3=255)["dim"], None)

check("parking brake set", body(b6=0x80)["parking_brake"], True)
check("parking brake clear", body(b6=0x7F)["parking_brake"], False)

# The door bits and the body bits share bytes 6 and 7 -- prove they coexist.
d = payload(rl=True, fl=True)
d[6] |= 0x80          # parking brake on, same byte as the rear-door bits
check("parking brake doesn't disturb doors",
      [k for k, v in RawOBD.decode_doors(d).items() if v], ["rl", "fl"])
check("doors don't disturb parking brake",
      RawOBD.decode_body(d)["parking_brake"], True)

print()
if fails:
    print("FAILED: %d" % len(fails))
else:
    print("ALL PASS")
sys.exit(1 if fails else 0)
