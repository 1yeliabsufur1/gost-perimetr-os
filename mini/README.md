# GOST MINI

A **portable OBD-II code reader + slow HUD** on a 2.13" e-paper screen — the
little sibling to the GOST/PERIMETR head unit. Battery powered, so you can
unplug it and carry the codes to the parts counter.

## Hardware
| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W | quad-core, plenty for this |
| 2.13" e-paper HAT (250×122) | Waveshare V2/V3/V4 all work |
| PiSugar | battery + the on-screen charge % |
| Bluetooth OBD-II adapter | OBDLink / ELM327, paired to `/dev/rfcomm0` |

## Why e-paper (and what it can't do)
A full refresh takes ~2 s and partials ~0.3 s, so **it cannot animate a
tachometer** — anything fast would ghost and wear the panel. What it's *great*
at is the opposite: it holds an image with **the power off**, and it's readable
in direct sunlight. So GOST MINI shows the things that suit it:

- **TROUBLE CODES** — the headline. Code + plain-English meaning, paged. Read
  them, unplug, walk into the shop with the screen still showing.
- **VITALS** — 12 V battery (hero number), coolant, fuel, range, hybrid charge.
- **GRAPH** — one sample every 5 s: coolant warm-up, voltage sag, charge drain.

## Install (on the Pi)
```bash
sudo apt install -y python3-pil python3-serial python3-gpiozero
pip3 install waveshare-epd     # or clone Waveshare's e-Paper repo
sudo rfcomm bind 0 <OBD_MAC>   # pair the adapter first with bluetoothctl
python3 gostmini.py
```

Run it at boot:
```bash
sudo cp gost-mini.service /etc/systemd/system/
sudo systemctl enable --now gost-mini
```

## Develop without the hardware
```bash
python3 gostmini.py --simulate     # writes PNG frames to sim/
```
The display layer auto-detects: no panel found → simulator. Every screen can be
designed and reviewed off-Pi.

## Controls
Optional HAT buttons (GPIO 5 / 6): next screen, and on CODES they page through.
Without buttons it auto-cycles (`--rotate N`, default 20 s; `0` disables).

## Shared with the main GOST
`describe_dtc()` comes from `../backend/dtc_lookup.py`, so a code reads
identically on the head unit and on MINI.
