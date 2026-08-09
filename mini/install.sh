#!/usr/bin/env bash
# GOST MINI installer -- run once on a fresh Raspberry Pi OS Lite (Pi Zero 2 W).
#
#   curl -fsSL https://raw.githubusercontent.com/1yeliabsufur1/gost-perimetr-os/main/mini/install.sh | bash
#
# Installs deps, enables SPI (the e-paper HAT needs it), fetches the Waveshare
# driver, installs GOST MINI to ~/gost-mini and starts it at boot.
set -Eeuo pipefail

REPO="https://github.com/1yeliabsufur1/gost-perimetr-os"
DEST="${GOST_MINI_DIR:-$HOME/gost-mini}"
USER_NAME="$(id -un)"

log()  { printf '\n\033[1m[gost-mini]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[gost-mini] WARNING: %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] && { echo "Run as your normal user (it will sudo when needed), not as root."; exit 1; }

log "installing packages (python, imaging, serial, bluetooth)"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  python3 python3-pip python3-pil python3-serial python3-numpy \
  python3-gpiozero python3-smbus2 \
  git bluez bluez-tools rfkill fonts-dejavu-core

log "enabling SPI (required by the e-paper HAT)"
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_spi 0 || warn "could not enable SPI via raspi-config"
else
  CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
  grep -q '^dtparam=spi=on' "$CFG" 2>/dev/null || echo 'dtparam=spi=on' | sudo tee -a "$CFG" >/dev/null
fi

log "fetching GOST MINI -> $DEST"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" pull --ff-only || warn "pull failed; keeping what's there"
else
  rm -rf "$DEST"
  TMP="$(mktemp -d)"
  # Plain shallow clone. (A sparse checkout looks tempting, but cone mode only
  # accepts DIRECTORIES -- passing backend/dtc_lookup.py made it fatal and
  # set -e killed the installer silently after enabling SPI.)
  git clone --depth 1 "$REPO" "$TMP/repo"
  mkdir -p "$DEST"
  cp -r "$TMP/repo/mini/." "$DEST/"
  # gostmini.py checks its own directory first, so the shared DTC table lives
  # right beside it (no stray ~/backend folder).
  cp "$TMP/repo/backend/dtc_lookup.py" "$DEST/dtc_lookup.py"
  rm -rf "$TMP"
fi

log "installing SPI/GPIO libs the e-paper driver needs"
sudo apt-get install -y --no-install-recommends python3-spidev python3-lgpio \
  || warn "spidev/lgpio install had trouble -- the panel may not open"

log "installing the Waveshare e-paper driver"
if [ ! -d "$DEST/waveshare_epd" ]; then
  # SPARSE clone of just the driver folder. The full e-Paper repo is >1GB of
  # STM32/Arduino demos and blows out /tmp (208MB tmpfs on a Zero) mid-checkout.
  # Cloning into $HOME (real disk) and checking out ONE directory avoids both.
  WS="$HOME/.gost-ws-src"
  rm -rf "$WS"
  if git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/waveshareteam/e-Paper "$WS" >/dev/null 2>&1 \
     && git -C "$WS" sparse-checkout set RaspberryPi_JetsonNano/python/lib/waveshare_epd >/dev/null 2>&1; then
    SRC="$WS/RaspberryPi_JetsonNano/python/lib/waveshare_epd"
    if [ -d "$SRC" ]; then
      # Install BESIDE the app: gostmini.py already puts its own dir on
      # sys.path, so this dodges site-packages and PEP-668 entirely.
      cp -r "$SRC" "$DEST/waveshare_epd"
      log "waveshare_epd installed to $DEST/waveshare_epd"
    else
      warn "unexpected Waveshare layout -- install the driver manually"
    fi
  else
    warn "could not fetch the Waveshare driver (no network?); MINI will run in simulate mode"
  fi
  rm -rf "$WS"
fi

log "installing the service"
sudo tee /etc/systemd/system/gost-mini.service >/dev/null <<UNIT
[Unit]
Description=GOST MINI (e-paper OBD code reader)
After=bluetooth.service network.target
Wants=bluetooth.service

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DEST
ExecStart=/usr/bin/python3 $DEST/gostmini.py
Restart=always
RestartSec=8

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable gost-mini.service

cat <<EOF

  GOST MINI installed to $DEST

  NEXT -- pair your Bluetooth OBD adapter (one time):
      bluetoothctl
        scan on            # find it, note the MAC (e.g. 00:1D:A5:xx:xx:xx)
        pair <MAC>
        trust <MAC>
        quit
      sudo rfcomm bind 0 <MAC>          # creates /dev/rfcomm0
      # make it stick across reboots:
      echo "rfcomm bind 0 <MAC>" | sudo tee /etc/rc.local.d-gost-mini >/dev/null

  THEN:
      sudo reboot                        # SPI needs a reboot to take effect

  After the reboot it starts automatically. Useful commands:
      systemctl status gost-mini
      journalctl -u gost-mini -f
      python3 $DEST/gostmini.py --simulate   # render screens to PNG, no panel

EOF
log "done -- reboot to enable SPI"
