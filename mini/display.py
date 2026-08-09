"""GOST MINI display layer -- 2.13" e-paper (250x122, 1-bit).

Wraps the Waveshare driver, and falls back to a PNG SIMULATOR when the real
panel isn't present, so screens can be designed/verified off-hardware.

E-paper reality that shapes everything here:
  * a FULL refresh takes ~2s and flashes black/white -- use it sparingly
  * a PARTIAL refresh is ~0.3s and is what we use for routine updates
  * too many partials in a row leave ghosting, so we force a full refresh
    every FULL_EVERY partials to clean the panel
Nothing here should be asked to draw faster than ~1 Hz.
"""
import os
import time

from PIL import Image, ImageDraw, ImageFont

W, H = 250, 122            # 2.13" panel, landscape
FULL_EVERY = 12            # clean the panel every N partial refreshes


def _font(size, bold=False):
    """DejaVu if present (Pi OS ships it), else PIL's builtin."""
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


FONT_XL = _font(34, True)     # the one big number
FONT_L = _font(20, True)
FONT_M = _font(14)
FONT_S = _font(11)
FONT_XS = _font(9)


class Display:
    def __init__(self, simulate=None, outdir="sim"):
        self.simulate = bool(simulate)
        self.epd = None
        self.partials = 0
        self.outdir = outdir
        self._frame = 0
        self.driver = None
        self.init_errors = []
        if not simulate:                      # None or False -> try real hardware
            # Fall back to the simulator if the panel can't be opened, and SAY SO
            # loudly. Previously a failed init left epd=None and every refresh
            # silently no-op'd, so the screen just kept its old image.
            if not self._init_panel():
                self.simulate = True
                print("[mini] NO E-PAPER PANEL -- falling back to simulate mode")
                for e in self.init_errors:
                    print("[mini]   " + e)
                print("[mini] run 'python3 diagnose.py' to see why")
        if self.simulate:
            os.makedirs(self.outdir, exist_ok=True)

    def _init_panel(self):
        """Try the Waveshare driver; several model revisions share this size."""
        try:
            import waveshare_epd  # noqa: F401
        except Exception as e:
            self.init_errors.append("waveshare_epd not importable: %s" % e)
            return False
        for mod in ("epd2in13_V4", "epd2in13_V3", "epd2in13_V2", "epd2in13"):
            try:
                m = __import__("waveshare_epd." + mod, fromlist=[mod])
                self.epd = m.EPD()
                self.epd.init()
                self.epd.Clear(0xFF)
                self.driver = mod
                print("[mini] e-paper ready via %s" % mod)
                return True
            except Exception as e:
                self.init_errors.append("%s: %s" % (mod, e))
                self.epd = None
                continue
        return False

    def blank(self):
        """A fresh white 1-bit canvas + its ImageDraw."""
        img = Image.new("1", (W, H), 255)
        return img, ImageDraw.Draw(img)

    def show(self, img, full=False):
        """Push a frame. Auto-promotes to a full refresh periodically so
        ghosting doesn't build up."""
        if self.partials >= FULL_EVERY:
            full = True
        if self.simulate:
            self._frame += 1
            img.save(os.path.join(self.outdir, "frame_%03d.png" % self._frame))
            self.partials = 0 if full else self.partials + 1
            return
        try:
            buf = self.epd.getbuffer(img.rotate(0))
            if full or not hasattr(self.epd, "displayPartial"):
                if hasattr(self.epd, "init"):
                    try:
                        self.epd.init()
                    except Exception:
                        pass
                self.epd.display(buf)
                self.partials = 0
            else:
                self.epd.displayPartial(buf)
                self.partials += 1
        except Exception as e:
            print("[mini] display error:", e)

    def sleep(self):
        """Deep-sleep the panel -- the image STAYS on screen with no power,
        which is the whole point of e-paper for a portable code reader."""
        try:
            if self.epd:
                self.epd.sleep()
        except Exception:
            pass


# ----------------------------------------------------------------- helpers ---

def header(d, title, right=""):
    """Inverse title bar across the top -- readable at a glance."""
    d.rectangle([0, 0, W, 16], fill=0)
    d.text((3, 2), title[:28], font=FONT_S, fill=255)
    if right:
        w = d.textlength(right, font=FONT_S)
        d.text((W - w - 3, 2), right, font=FONT_S, fill=255)


def footer(d, text):
    d.text((3, H - 11), text[:46], font=FONT_XS, fill=0)


def wrap(d, text, font, maxw):
    """Greedy word-wrap to pixel width."""
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines
