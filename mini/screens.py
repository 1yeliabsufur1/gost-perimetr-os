"""GOST MINI screens for the 2.13" e-paper panel.

Everything here is designed around SLOW data -- the values bailey called out
(12V battery, coolant, state of charge, range, PiSugar %) drift over seconds,
which is exactly what e-paper is good at. Nothing tries to animate.
"""
from display import (W, H, FONT_XL, FONT_L, FONT_M, FONT_S, FONT_XS,
                     header, footer, wrap)


def _fmt(v, dp=0, dash="--"):
    if v is None:
        return dash
    try:
        return ("%." + str(dp) + "f") % float(v)
    except (TypeError, ValueError):
        return dash


def screen_boot(disp, msg="starting..."):
    img, d = disp.blank()
    d.text((10, 26), "GOST", font=FONT_XL, fill=0)
    d.text((10, 62), "MINI", font=FONT_L, fill=0)
    d.text((10, 88), msg, font=FONT_S, fill=0)
    d.line([(0, H - 1), (W, H - 1)], fill=0)
    return img


def screen_codes(disp, codes, page=0, batt=None):
    """THE headline screen: trouble codes + plain-English meaning. E-paper
    holds this with the power off, so you can carry it to the parts counter."""
    img, d = disp.blank()
    n = len(codes)
    header(d, "TROUBLE CODES", ("BAT %s%%" % _fmt(batt)) if batt is not None else "")
    if not n:
        d.text((10, 44), "NO CODES", font=FONT_L, fill=0)
        d.text((10, 68), "Engine systems OK", font=FONT_S, fill=0)
        footer(d, "hold BTN: re-scan")
        return img
    per = 2                                  # 2 codes per page: code + wrapped text
    pages = (n + per - 1) // per
    page %= pages
    y = 20
    for c in codes[page * per:page * per + per]:
        code = c.get("code", "?")
        kind = (c.get("kind") or "").upper()[:4]
        d.text((3, y), code, font=FONT_L, fill=0)
        if kind:
            d.text((3 + d.textlength(code, font=FONT_L) + 6, y + 6), kind, font=FONT_XS, fill=0)
        y += 21
        for line in wrap(d, c.get("desc", ""), FONT_S, W - 8)[:2]:
            d.text((5, y), line, font=FONT_S, fill=0)
            y += 12
        y += 4
    footer(d, "%d code(s)  page %d/%d" % (n, page + 1, pages))
    return img


def screen_vitals(disp, v, batt=None):
    """Slow-moving vitals in a 2x2 grid + a hero value. All of these update on
    the order of seconds, so partial refreshes look fine."""
    img, d = disp.blank()
    header(d, "VITALS", ("BAT %s%%" % _fmt(batt)) if batt is not None else "")

    # hero: 12V battery -- the number that actually strands you
    d.text((4, 20), "12V BATTERY", font=FONT_XS, fill=0)
    d.text((4, 30), _fmt(v.get("volts"), 1) + "V", font=FONT_XL, fill=0)

    cells = [
        ("COOLANT", (_fmt(v.get("coolant_f")) + "°F") if v.get("coolant_f") is not None else "--"),
        ("FUEL", (_fmt(v.get("fuel")) + "%") if v.get("fuel") is not None else "--"),
        ("RANGE", (_fmt(v.get("range_mi")) + " mi") if v.get("range_mi") is not None else "--"),
        ("CHARGE", (_fmt(v.get("soc")) + "%") if v.get("soc") is not None else "--"),
    ]
    x0, y0 = 128, 20
    for i, (label, val) in enumerate(cells):
        cx = x0 + (i % 2) * 61
        cy = y0 + (i // 2) * 44
        d.rectangle([cx, cy, cx + 57, cy + 40], outline=0)
        d.text((cx + 4, cy + 3), label, font=FONT_XS, fill=0)
        d.text((cx + 4, cy + 16), val, font=FONT_M, fill=0)
    footer(d, "BTN: next screen")
    return img


def screen_graph(disp, samples, label, batt=None):
    """A SLOW rolling graph (one sample every few seconds). Perfect for coolant
    warm-up, charge drain, voltage sag -- useless for RPM, so we don't try."""
    img, d = disp.blank()
    pts = [s for s in samples if s is not None]
    cur = pts[-1] if pts else None
    header(d, label[:18], ("BAT %s%%" % _fmt(batt)) if batt is not None else "")
    if len(pts) < 2:
        d.text((10, 50), "collecting...", font=FONT_M, fill=0)
        footer(d, "one sample every 5s")
        return img
    lo, hi = min(pts), max(pts)
    if hi == lo:
        hi = lo + 1
    # current value in its own left column; min/max ride just inside the plot
    # so nothing overlaps the big number or the trace.
    d.text((2, 20), "NOW", font=FONT_XS, fill=0)
    d.text((2, 30), _fmt(cur, 1), font=FONT_L, fill=0)
    gx0, gy0, gx1, gy1 = 62, 22, W - 4, H - 16
    d.rectangle([gx0, gy0, gx1, gy1], outline=0)
    d.text((gx0 + 3, gy0 + 2), _fmt(hi, 0), font=FONT_XS, fill=0)
    d.text((gx0 + 3, gy1 - 10), _fmt(lo, 0), font=FONT_XS, fill=0)
    n = len(samples)
    prev = None
    for i, s in enumerate(samples):
        if s is None:
            prev = None
            continue
        x = gx0 + int((gx1 - gx0) * (i / max(1, n - 1)))
        y = gy1 - int((gy1 - gy0) * ((s - lo) / (hi - lo)))
        if prev:
            d.line([prev, (x, y)], fill=0, width=2)
        prev = (x, y)
    footer(d, "5s samples  ·  BTN: next")
    return img


def screen_no_link(disp, detail="", batt=None):
    img, d = disp.blank()
    header(d, "NO OBD LINK", ("BAT %s%%" % _fmt(batt)) if batt is not None else "")
    d.text((8, 30), "Searching for", font=FONT_M, fill=0)
    d.text((8, 48), "Bluetooth OBD...", font=FONT_M, fill=0)
    for i, line in enumerate(wrap(d, detail, FONT_XS, W - 12)[:2]):
        d.text((8, 74 + i * 11), line, font=FONT_XS, fill=0)
    footer(d, "key on  ·  adapter paired?")
    return img
