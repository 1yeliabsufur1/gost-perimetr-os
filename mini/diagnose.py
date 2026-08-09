#!/usr/bin/env python3
"""GOST MINI hardware diagnostic.

Answers "why is the e-paper not updating?" by checking each link in the chain
and, if the panel does open, drawing an unmistakable test pattern.

    python3 diagnose.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"


def check(label, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, "%s: %s" % (type(e).__name__, e)
    print("%s %-26s %s" % (OK if ok else BAD, label, detail))
    return ok


def c_spi_config():
    for cfg in ("/boot/firmware/config.txt", "/boot/config.txt"):
        if os.path.exists(cfg):
            with open(cfg) as f:
                txt = f.read()
            on = any(l.strip().startswith("dtparam=spi=on") for l in txt.splitlines())
            return on, ("dtparam=spi=on in %s" % cfg) if on else \
                ("SPI NOT enabled in %s -> sudo raspi-config nonint do_spi 0 && sudo reboot" % cfg)
    return False, "no config.txt found"


def c_spi_dev():
    devs = [d for d in os.listdir("/dev") if d.startswith("spidev")] if os.path.isdir("/dev") else []
    return bool(devs), (", ".join("/dev/" + d for d in devs) if devs
                        else "no /dev/spidev* -- enable SPI then REBOOT")


def c_pil():
    import PIL
    return True, "Pillow " + PIL.__version__


def c_driver():
    import waveshare_epd
    p = getattr(waveshare_epd, "__file__", "?")
    mods = []
    d = os.path.dirname(p) if p else ""
    if d and os.path.isdir(d):
        mods = sorted(m[:-3] for m in os.listdir(d) if m.startswith("epd2in13") and m.endswith(".py"))
    return True, "installed (%s); 2.13\" modules: %s" % (d, ", ".join(mods) or "NONE")


def c_gpio_lib():
    try:
        import gpiozero  # noqa: F401
        return True, "gpiozero present"
    except Exception:
        return False, "gpiozero missing (buttons disabled; display still works)"


def c_serial():
    import glob
    hits = sorted(glob.glob("/dev/rfcomm*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return bool(hits), (", ".join(hits) if hits
                        else "no OBD device -- pair it, then: sudo rfcomm bind 0 <MAC>")


def c_serial_access():
    """/dev/rfcomm0 is root:dialout -- the service user must be in that group
    or opening the adapter fails with EACCES (looks exactly like 'no link')."""
    import glob, grp, os, pwd
    hits = sorted(glob.glob("/dev/rfcomm*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not hits:
        return False, "no port to test (pair the adapter first)"
    me = pwd.getpwuid(os.getuid()).pw_name
    groups = [g.gr_name for g in grp.getgrall() if me in g.gr_mem]
    readable = os.access(hits[0], os.R_OK | os.W_OK)
    if readable:
        return True, "%s is read/write for %s" % (hits[0], me)
    return False, ("%s NOT accessible by %s (groups: %s) -> "
                   "sudo usermod -aG dialout %s && sudo systemctl restart gost-mini"
                   % (hits[0], me, ",".join(groups) or "none", me))


def c_pisugar():
    try:
        import pisugar
        v = pisugar.battery_pct()
        return v is not None, ("battery %s%%" % v) if v is not None else "no PiSugar server on :8423"
    except Exception as e:
        return False, str(e)


def main():
    print("\n=== GOST MINI diagnostic ===\n")
    print("-- system --")
    check("SPI enabled in config", c_spi_config)
    check("/dev/spidev* present", c_spi_dev)
    print("\n-- python libs --")
    check("Pillow (imaging)", c_pil)
    check("waveshare_epd driver", c_driver)
    check("gpiozero (buttons)", c_gpio_lib)
    print("\n-- peripherals --")
    check("OBD serial port", c_serial)
    check("OBD port permissions", c_serial_access)
    check("PiSugar battery", c_pisugar)

    print("\n-- panel --")
    # The service holds SPI/GPIO while it runs, so a second process gets
    # "GPIO busy" -- that's contention, not a hardware fault. Say so plainly.
    try:
        svc = subprocess.run(["systemctl", "is-active", "gost-mini"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        svc = ""
    if svc == "active":
        print("%s gost-mini is RUNNING and owns the panel (that's why you'd see" % WARN)
        print("       'GPIO busy'). To drive the panel from here:")
        print("         sudo systemctl stop gost-mini && python3 diagnose.py")
        print("         sudo systemctl start gost-mini")
        return 0
    from display import Display
    d = Display()                      # prints its own reason if it falls back
    if d.simulate:
        print("%s panel did NOT open -- see the failures above" % BAD)
        for e in d.init_errors:
            print("       " + e)
        print("\nMost common fixes:")
        print("  1. SPI off or no reboot:  sudo raspi-config nonint do_spi 0 && sudo reboot")
        print("  2. driver missing:        re-run mini/install.sh")
        print("  3. HAT not seated fully on the 40-pin header")
        return 1

    print("%s panel open via %s -- drawing a test pattern" % (OK, d.driver))
    img, dr = d.blank()
    from display import W, H, FONT_L, FONT_S
    dr.rectangle([0, 0, W - 1, H - 1], outline=0)
    dr.rectangle([0, 0, W, 18], fill=0)
    dr.text((4, 3), "GOST MINI - TEST", font=FONT_S, fill=255)
    dr.text((8, 28), "IF YOU CAN", font=FONT_L, fill=0)
    dr.text((8, 52), "READ THIS,", font=FONT_L, fill=0)
    dr.text((8, 76), "IT WORKS", font=FONT_L, fill=0)
    for i in range(0, W, 8):           # checker strip proves full-width refresh
        dr.rectangle([i, H - 14, i + 3, H - 4], fill=0)
    d.show(img, full=True)
    print("%s test pattern sent -- look at the panel now" % OK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
