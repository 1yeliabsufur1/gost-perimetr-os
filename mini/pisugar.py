"""PiSugar battery readout for GOST MINI.

The PiSugar server exposes a tiny TCP text API on 127.0.0.1:8423
("get battery" -> "battery: 87.5"). Falls back to its I2C gauge if the
server isn't running. All best-effort: no PiSugar just means no % shown.
"""
import socket


def _ask(cmd, timeout=0.6):
    try:
        s = socket.create_connection(("127.0.0.1", 8423), timeout=timeout)
        s.settimeout(timeout)
        s.sendall((cmd + "\n").encode())
        data = s.recv(256).decode(errors="ignore")
        s.close()
        return data.strip()
    except Exception:
        return ""


def battery_pct():
    """0-100 int, or None when no PiSugar is present."""
    r = _ask("get battery")
    if ":" in r:
        try:
            return int(round(float(r.split(":", 1)[1].strip())))
        except ValueError:
            pass
    # I2C fallback (PiSugar 2/3 fuel gauges live at 0x57 / 0x32)
    try:
        from smbus2 import SMBus
        with SMBus(1) as bus:
            for addr in (0x57, 0x32):
                try:
                    v = bus.read_byte_data(addr, 0x2A)
                    if 0 <= v <= 100:
                        return int(v)
                except Exception:
                    continue
    except Exception:
        pass
    return None


def charging():
    r = _ask("get battery_charging")
    return "true" in r.lower() if r else None
