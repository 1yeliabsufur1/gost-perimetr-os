#!/usr/bin/env python3
"""GOST MINI -- portable OBD code reader + slow HUD.

Raspberry Pi Zero 2 W + 2.13" e-paper HAT + PiSugar + a Bluetooth OBD adapter.

Why e-paper suits this: a full refresh is ~2s, so it can't animate a tachometer
-- but it holds an image with the power OFF and is readable in direct sun. That
makes it ideal for the things that matter here: trouble codes you can carry to
the parts counter, and slow vitals (12V, coolant, charge, range).

Run:
  python3 gostmini.py                 # on the Pi (auto-detects the panel)
  python3 gostmini.py --simulate      # anywhere: writes PNG frames to sim/
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Reuse the main GOST DTC database so codes read the same on both units.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import screens
from display import Display
from obd import MiniOBD

try:
    from dtc_lookup import describe_dtc
except Exception:
    def describe_dtc(code):
        return ""

try:
    import pisugar
except Exception:
    pisugar = None

SCREENS = ["codes", "vitals", "graph"]
GRAPH_CHOICES = [
    ("coolant_f", "COOLANT F"),
    ("volts", "12V BATTERY"),
    ("soc", "CHARGE %"),
    ("fuel", "FUEL %"),
]
SAMPLE_SECS = 5           # slow on purpose -- see module docstring
MAX_SAMPLES = 46          # ~4 minutes of history across the plot
CODE_RESCAN_SECS = 120


class Mini:
    def __init__(self, simulate=False, rotate_secs=0):
        self.disp = Display(simulate=simulate)
        self.obd = MiniOBD()
        self.screen = 0
        self.graph_choice = 0
        self.page = 0
        self.codes = []
        self.vitals = {}
        self.samples = []
        self.last_codes = 0.0
        self.rotate_secs = rotate_secs
        self.last_rotate = time.time()
        self.batt = None

    # ---------------------------------------------------------------- data --
    def battery(self):
        if pisugar is None:
            return None
        try:
            return pisugar.battery_pct()
        except Exception:
            return None

    def refresh_codes(self):
        raw = self.obd.read_codes()
        self.codes = [{"code": c, "kind": k, "desc": describe_dtc(c)} for c, k in raw]
        self.last_codes = time.time()

    def sample(self):
        self.vitals = self.obd.read_vitals()
        key = GRAPH_CHOICES[self.graph_choice][0]
        self.samples.append(self.vitals.get(key))
        if len(self.samples) > MAX_SAMPLES:
            self.samples.pop(0)

    # -------------------------------------------------------------- render --
    def draw(self, full=False):
        name = SCREENS[self.screen]
        if not self.obd.alive():
            img = screens.screen_no_link(self.disp, self.obd.detail, self.batt)
        elif name == "codes":
            img = screens.screen_codes(self.disp, self.codes, self.page, self.batt)
        elif name == "vitals":
            img = screens.screen_vitals(self.disp, self.vitals, self.batt)
        else:
            key, label = GRAPH_CHOICES[self.graph_choice]
            img = screens.screen_graph(self.disp, self.samples, label, self.batt)
        self.disp.show(img, full=full)

    def next_screen(self):
        self.screen = (self.screen + 1) % len(SCREENS)
        # cycling onto the graph steps which value it plots
        if SCREENS[self.screen] == "graph":
            self.graph_choice = (self.graph_choice + 1) % len(GRAPH_CHOICES)
            self.samples = []
        self.page = 0
        self.draw(full=True)

    # ---------------------------------------------------------------- loop --
    def run(self):
        self.disp.show(screens.screen_boot(self.disp, "linking to OBD..."), full=True)
        buttons = setup_buttons(self)
        if buttons:
            print("[mini] GPIO buttons ready")
        try:
            while True:
                if not self.obd.alive():
                    if not self.obd.connect():
                        # Back off while nothing's there. This runs on a PiSugar
                        # battery, so retrying every 5s with the truck switched
                        # off would flatten it for no reason. 5s -> 60s.
                        self._miss = min(getattr(self, "_miss", 0) + 1, 12)
                        self.batt = self.battery()
                        self.draw()
                        time.sleep(min(5 * self._miss, 60))
                        continue
                    # Fresh link -- e.g. the truck just started again. Re-read
                    # the codes and drop stale samples from the last drive.
                    self._miss = 0
                    self.samples = []
                    self.refresh_codes()
                    self.draw(full=True)

                self.batt = self.battery()
                self.sample()
                if time.time() - self.last_codes > CODE_RESCAN_SECS:
                    self.refresh_codes()
                if self.rotate_secs and time.time() - self.last_rotate > self.rotate_secs:
                    self.last_rotate = time.time()
                    self.next_screen()
                else:
                    self.draw()
                time.sleep(SAMPLE_SECS)
        except KeyboardInterrupt:
            pass
        finally:
            self.disp.sleep()      # image persists with the power off


def setup_buttons(mini):
    """Optional GPIO buttons (many 2.13" HATs expose 4 on 5/6/13/19).
    Short press = next screen, on CODES it pages through them."""
    try:
        from gpiozero import Button
    except Exception:
        return None
    try:
        btns = []
        b1 = Button(5, pull_up=True, bounce_time=0.1)
        b1.when_pressed = lambda: (
            setattr(mini, "page", mini.page + 1), mini.draw(full=True)
        ) if SCREENS[mini.screen] == "codes" else mini.next_screen()
        btns.append(b1)
        b2 = Button(6, pull_up=True, bounce_time=0.1)
        b2.when_pressed = mini.next_screen
        btns.append(b2)
        return btns
    except Exception as e:
        print("[mini] buttons unavailable:", e)
        return None


def main():
    ap = argparse.ArgumentParser(description="GOST MINI e-paper OBD reader")
    ap.add_argument("--simulate", action="store_true", help="render PNGs to sim/ instead of a panel")
    ap.add_argument("--rotate", type=int, default=20,
                    help="auto-cycle screens every N seconds (0 = manual only)")
    ap.add_argument("--clean", action="store_true",
                    help="deep-scrub e-paper ghosting (black/white cycles) and exit")
    a = ap.parse_args()
    if a.clean:
        from display import Display, CLEAN_PASSES
        d = Display(simulate=a.simulate)
        print("[mini] deep cleaning (%d passes)..." % CLEAN_PASSES)
        d.deep_clean(CLEAN_PASSES)
        d.sleep()
        print("[mini] done -- the panel should be uniformly white")
        return
    Mini(simulate=a.simulate, rotate_secs=a.rotate).run()


if __name__ == "__main__":
    main()
