# GOST/PERIMETR OS

A retro Soviet-terminal / amber-CRT car head-unit for the Raspberry Pi 5: drive
telemetry over OBD2, a broadcast-simulation TV with a programmable guide,
music/podcasts, offline maps, and a kiosk UI driven by a 5-way pad or
keyboard. Runs fully offline after a one-time first-boot install.

## Hardware

- **Raspberry Pi 5 (8GB recommended)**
- **Official 27W USB-C power supply.** This is not optional. Multi-port "GaN
  120W" chargers negotiate only 5V/3A on the Pi 5's port, and under load that
  weak 5V rail causes EXT4 I/O errors and, on USB-booted setups, bootloader
  refusal. If you're booting from a USB SSD instead of a microSD card, the
  27W supply matters even more — microSD is immune to the USB current
  budget, USB-attached storage is not.
- microSD card (32GB+) or USB SSD
- OBDLink EX or any ELM327-compatible USB OBD2 adapter (`/dev/ttyUSB*`)
- Optional: 5-way button pad (GPIO), MCP3008 + potentiometers for
  volume/brightness (SPI), NMEA GPS module (serial), Pi Camera for dashcam
- A small display + a way to drive it in kiosk mode (car head-unit screen,
  HDMI panel, etc.)

## Flashing

1. Grab `gost-perimetr-os.img.xz` from this repo's `build-image` GitHub
   Actions workflow (Actions tab → latest run → Artifacts).
2. Open **Raspberry Pi Imager** → *Choose Device* → Raspberry Pi 5 →
   *Choose OS* → scroll to **Use custom** → pick the `.img.xz` (no need to
   unpack it, Imager reads xz directly) → *Choose Storage* → **Write**.
3. When Imager asks "Would you like to apply OS customisation settings?"
   answer **No / don't apply**. Trixie configures itself via cloud-init on
   first boot; setting a username/password/Wi-Fi through Imager's dialog can
   race that provisioning and land you on a broken sudo prompt at first
   login. That's upstream cloud-init behavior — the image is fully
   self-configuring through `gost-firstboot.service`, so customization is
   never needed anyway. Pick device, pick the image, write — that's it.
4. Boot the Pi with Ethernet connected for a fully hands-off first boot. For
   Wi-Fi-only first boot, you'll need one manual step — see below.

## First boot

**The prebuilt image is "fat" — all dependencies (cage, Chromium, mpv,
ffmpeg, the Python environment) are baked in at build time.** So first boot
needs NO internet and finishes in seconds: `gost-firstboot.service` sees the
`/opt/gost/.provisioned` marker, skips apt/pip entirely, does a quick
offline hardware-wiring pass (detect user, template + enable the two
services, media skeleton, Wi-Fi unblock), and reboots straight into the
kiosk. No network/clock/apt-signature failure is even possible on this path.

If you instead build a *thin* image (or run `install.sh` on a stock Pi OS),
the installer waits for network **and for the clock to NTP-sync** — the Pi
has no battery clock, and without that wait apt rejects repo signatures as
"not live until \<future date>" and dies — then installs everything online
(~10 minutes) before its first reboot into the kiosk.

- **Default login:** `gost` / `gost` (pre-seeded — you'll never see Pi OS's
  "create a user" prompt). The kiosk's setup wizard replaces this password
  on first run; change it there, not by hand.
- **Ethernet:** fully hands-off, no action needed.
- **Wi-Fi only:** SSH in during the first boot window (or attach a keyboard)
  and run:
  ```
  sudo nmcli dev wifi connect "<SSID>" password "<PASSWORD>"
  ```
  then let `install.sh` continue (it's watching for `network-online.target`
  either way).

After the reboot that ends `install.sh`, you land in the GOST/PERIMETR OS
first-run setup wizard: create an operator password (this becomes your
system/SSH password), then choose whether to connect to the internet now.
Every boot after that goes straight to the boot animation and HOME.

### If you end up at a login prompt instead of the dashboard

That means the first-boot install didn't finish (usually: no internet, or a
badly skewed clock on an older image). It is always recoverable without
re-flashing:

1. Log in (`gost` / `gost`, or whatever user you created).
2. Check what happened: `systemctl status gost-firstboot` and
   `journalctl -u gost-firstboot -e`.
3. Get network up if needed (`sudo nmcli dev wifi connect ...`), then just
   re-run the installer — it's idempotent and picks up where it left off:
   ```
   sudo /opt/gost-src/install.sh
   ```
   It reboots into the kiosk when it finishes.

**Do not install X11/Xorg/LightDM/openbox "to fix the GUI".** There is no X
in this OS and `startx` will never exist — the interface is Wayland
(`cage` + Chromium), installed and started by the steps above. Bolting a
desktop stack on top just wastes space and fights the kiosk service for the
display.

## Getting media onto the unit

SSH in (headless rescue is always available — `boot/ssh` ships enabled) and
drop files under `/opt/gost/media/`:

```
/opt/gost/media/TV/03 NEWS/*.mp4        # channel 3, named "NEWS"
/opt/gost/media/TV/07 CARTOONS/*.mp4    # channel 7
/opt/gost/media/TV/COMMERCIALS/*.mp4    # gap-filler spots, shown as channel 00 in GUIDE
/opt/gost/media/MUSIC/ROCK/*.mp3        # music, grouped by genre for quick browsing
/opt/gost/media/PODCASTS/NEWS/*.mp3     # podcasts are a separate pool from music, same genre convention
```

Channel folders must be named `NN NAME` with `NN` between 03 and 82. Music
and podcasts are grouped by genre using the first folder level under
`MUSIC/`/`PODCASTS/` (files dropped directly in the pool root show up under
"UNSORTED"). A USB stick with a `TV/`, `MUSIC/`, or `PODCASTS/` folder at its
root is auto-merged within a few seconds of being plugged in — no folder
naming required on the stick itself beyond that.

Offline maps: copy a `.pmtiles` file into `/opt/gost/maps/`. Free region
extracts are available at protomaps.com/downloads and use the same layer
schema (earth/water/landuse/buildings/roads/boundaries) NAV expects, so they
render correctly out of the box. Without a map file, NAV shows a graceful
fallback panel instead of a blank map.

**Turn-by-turn routing** runs entirely offline: click the map (or, on the
5-way pad, press SET DESTINATION, pan to your target, and press Enter to
confirm) and GOST builds a routable graph directly from the road geometry in
the currently loaded map tiles, then finds the shortest route by travel time
and shows a turn list plus a live "next turn" banner that advances as your
GPS fix moves. Two honest limitations worth knowing: it only routes through
road data in tiles that are actually loaded (pan/zoom so both ends of your
trip are visible before setting a destination), and it has no live traffic
and doesn't enforce one-way streets (the Protomaps basemap schema doesn't
reliably carry that information) — treat it as a solid offline routing MVP,
not a replacement for a full commercial routing engine.

## Controls

5-way pad (GPIO) and keyboard arrows/Enter behave identically:

| Action | Effect |
|---|---|
| Up / Down | Move focus within the current tab |
| Left / Right | Switch tabs |
| Enter | Activate the focused item |
| Hold Left 5s (or Escape) | Universal back — closes any overlay browser, exits TV, kills launched apps |
| (while watching TV) Up / Down | Step channel |
| (while watching TV) Enter | Pause/resume |

Text fields always take priority: while an input/textarea is focused, none
of the above hotkeys fire — typing (including Backspace) goes straight to
the field. PageUp/PageDown are direct channel up/down while watching TV —
IR remotes on USB receivers show up as keyboards, so most of them work out
of the box.

### 5-way pad wiring (GPIO)

Buttons are **active-low**: each button connects its GPIO pin to ground
(internal pull-ups are enabled in software — no external resistors needed).
All five signals sit on consecutive odd physical pins along one edge of the
40-pin header, so a single-row connector covers the whole pad:

| Button | BCM GPIO | Physical pin |
|---|---|---|
| Up | GPIO 5 | 29 |
| Down | GPIO 6 | 31 |
| Left (hold 5s = back) | GPIO 13 | 33 |
| Right | GPIO 19 | 35 |
| Enter | GPIO 26 | 37 |
| Ground (shared) | — | 30, 34, or 39 |

If your PCB routes different pins, no code change needed — add a
`"gpio_pins": {"up": 5, "down": 6, "left": 13, "right": 19, "enter": 26}`
override to `/opt/gost/state/config.json` and restart `hud-backend`.

## Guide rules (the TV engine)

- Durations are always real — probed with `ffprobe` on-device (cached by
  path + mtime) and via in-browser video metadata probing in standalone/demo
  mode. Nothing is ever assumed to be 30 minutes.
- Overlap is impossible: the GUIDE picker greys out start slots that would
  collide with an existing block, and the backend re-sanitizes the whole
  schedule on every save as a second line of defense.
- Tuning into a block mid-way starts playback at the correct wall-clock
  offset (a 5:00–5:30 block tuned at 5:12 starts 12 minutes in). Unscheduled
  channels run a continuous pseudo-broadcast rotation seeded by epoch time,
  so different units (or a unit re-tuned later) join mid-stream consistently.
- Gaps between scheduled blocks play a random commercial, hard-cut so it
  never overruns into the next show. No commercials on hand → animated
  static instead. An entirely empty channel is simply off-air (static).
- TV itself is watch-only; all file management and scheduling happens in
  GUIDE.

## Vehicle telemetry / OBD-II

Plug any ELM327-compatible USB adapter (OBDLink EX recommended) into the Pi
— it shows up as `/dev/ttyUSB0` and the backend picks it up automatically,
no pairing step. You get live SPEED/RPM/boost/temps/12V at 5Hz, fuel or
hybrid-battery level, and a **stored trouble-code (DTC) sweep every 30
seconds** — codes appear in the HUD tab's DTC panel with first-seen
timestamps. Vehicle type (gas / hybrid / EV) is auto-detected from which
PIDs answer, and the DRIVE/HUD labels adapt accordingly. If no adapter is
present the DRIVE tab shows NOT CONNECTED and everything else works
normally.

## Showcase mode

There is no separate "demo build" — the flashed unit always runs the real
OS. In **AUTO** mode, real OBD data always wins: the backend hunts for the
adapter across every serial port and baud rate, and only falls back to a
simulated drive if no real link can be made (so the screen stays alive) —
switching to real data the instant the adapter links up. The DRIVE tab shows
a **● LIVE — OBDLink** banner when it's on the real truck, or **◌ SHOWCASE**
when simulated, so it's never ambiguous. Settings → SOURCE → **SHOWCASE**
forces the simulation for showing the unit off indoors.

## Safe mode (break a boot loop) & OBD testing

If the kiosk ever misbehaves, you can always get a console:

- **Force safe mode** — the kiosk is skipped and you boot to a login prompt:
  - From the Pi: `touch /boot/gost_safe_mode && sudo reboot`
  - From a PC: put an empty file named `gost_safe_mode` on the SD card's
    boot partition (the `bootfs` drive).
  - Remove the file (`sudo rm /boot/gost_safe_mode`) and reboot to return.
- **Consoles are always on tty2–tty6** — press **Ctrl+Alt+F2** any time for a
  login even while the kiosk owns tty1.
- The kiosk has a **crash-loop breaker**: >6 restarts in 120s and it stops
  itself with on-screen instructions instead of flashing forever.
- **Test the OBD link directly:**
  ```
  /opt/gost/venv/bin/python3 /opt/gost/tools/test_obd.py
  ```
  It scans every port/baud, and on a working link dumps live RPM/speed/
  coolant plus stored trouble codes — the definitive "is it the adapter, the
  truck, or the software" check.
- **Watch the backend live:** `journalctl -u hud-backend -f` (it logs every
  OBD connection attempt, the protocol, and the full supported-PID list).

## Testing in a VM (VirtualBox etc.)

There's no separate x86 `.iso` — the OS *is* Raspberry Pi OS plus this
repo's installer, and a bootable PC ISO would be a different distribution
entirely. Two good ways to test without a Pi:

1. **Frontend only (zero setup):** open `app/index.html` in any browser.
   You get the full UI in preview mode with simulated telemetry.
2. **Full stack:** install stock Debian 13 (trixie) amd64 in VirtualBox,
   then `git clone` this repo and run `sudo ./install.sh`. Everything works
   except GPIO/OBD/camera hardware (the installer skips the Pi-only lgpio
   package automatically on non-Pi systems). The kiosk boots into the same
   cage + Chromium shell.

## Emulation (optional add-on)

Retro console emulation is **not** part of the base image — it's kept as a
documented, opt-in add-on so the base flash stays small and boots fast. If
you want it, install a RetroArch/EmulationStation stack yourself after first
boot; it isn't wired into the kiosk shell here.

## Pi Zero 2 W

Under evaluation, not supported yet. Chromium in kiosk mode is too heavy for
512MB of RAM — it either OOMs or is unusably slow. Stick to the Pi 5 (or at
minimum a Pi 4B with 4GB+) until that's revisited.

## Repo layout

```
app/          Frontend: single self-contained app/index.html + vendored MapLibre/PMTiles
backend/      hud_server.py -- the entire backend, single asyncio file
system/       systemd units, kiosk-start.sh, gost-setpass, sudoers fragment
media/        Empty TV/MUSIC skeleton shipped in the image
install.sh    Idempotent first-boot installer
.github/workflows/build-image.yml   Builds a flashable image via GitHub Actions
```
