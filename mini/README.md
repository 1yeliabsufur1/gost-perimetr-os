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

## Setup — flash the ready-made image

The easiest path: flash `gost-mini.img.xz` from the
[latest release](https://github.com/1yeliabsufur1/gost-perimetr-os/releases).
It has the code, services, SPI/I2C and SSH already baked in — first boot
installs the remaining packages and the e-paper driver, then the panel comes
up on its own.

1. [Raspberry Pi Imager](https://www.raspberrypi.com/software/) →
   **Choose OS → Use custom** → pick `gost-mini.img.xz`
2. Click the **⚙ gear / Edit Settings** and set **hostname** (`gostmini`),
   **enable SSH**, your **username/password**, and **Wi-Fi + country**
   *(the country matters — without it the radio stays off)*
3. Write, boot, wait ~3 minutes for first-boot setup
4. `ssh <user>@gostmini.local`, then pair the adapter **at the vehicle with the
   ignition on**: `cd ~/gost-mini && ./pair-obd.sh`

Building the image yourself: `sudo tools/build-mini-image.sh` (needs a
Raspberry Pi OS Lite arm64 base image in `/root/minibuild/raspios.img.xz`).

## Manual setup (existing Raspberry Pi OS install)

A Pi Zero 2 W is awkward to plug a keyboard into, so do the whole thing over
Wi-Fi + SSH. **Raspberry Pi Imager sets that up for you before the card is even
written** — no custom image required.

### 1. Write the card
[Raspberry Pi Imager](https://www.raspberrypi.com/software/) →
**Device:** Raspberry Pi Zero 2 W · **OS:** Raspberry Pi OS **Lite (64-bit)** ·
**Storage:** your SD card.

Then click the **⚙ gear / "Edit Settings"** and set:

| Setting | Value |
|---|---|
| Hostname | `gostmini` |
| Enable SSH | ✅ (use password authentication) |
| Username / password | your choice — you'll SSH with these |
| Configure wireless LAN | your Wi-Fi SSID + password |
| Wireless LAN country | your country (**required**, or the radio stays off) |

Write the card, put it in the Pi, power it up, and wait ~60 s for first boot.

### 2. SSH in
```bash
ssh <username>@gostmini.local
```
(If the name doesn't resolve, find the IP in your router's client list and use
that instead.)

### 3. One command installs everything
```bash
curl -fsSL https://raw.githubusercontent.com/1yeliabsufur1/gost-perimetr-os/main/mini/install.sh | bash
```
That installs the deps, enables SPI for the HAT, fetches the Waveshare driver,
installs GOST MINI, and enables it at boot. Then `sudo reboot` (SPI needs it).

### 4. Pair the OBD adapter (one time)
**Plug the adapter into the OBD port and turn the key to accessory first** — it
is powered by the car, so it doesn't broadcast otherwise. Then:

```bash
cd ~/gost-mini && ./pair-obd.sh
```
It finds the adapter by name, pairs, trusts, binds `/dev/rfcomm0`, and installs
a unit so the bind survives reboots. No MAC typing. If auto-detection misses
it, pass one: `./pair-obd.sh AA:BB:CC:DD:EE:FF`

Check on it any time:
```bash
systemctl status gost-mini
journalctl -u gost-mini -f
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
