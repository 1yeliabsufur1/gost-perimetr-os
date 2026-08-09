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
  git bluez bluez-tools bluez-hcidump rfkill fonts-dejavu-core

log "granting serial access (dialout) -- /dev/rfcomm0 is root:dialout, so the"
log "service user can't open the OBD adapter without this"
sudo usermod -aG dialout "$USER_NAME" || warn "could not add $USER_NAME to dialout"

log "enabling SPI (required by the e-paper HAT)"
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_spi 0 || warn "could not enable SPI via raspi-config"
else
  CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
  grep -q '^dtparam=spi=on' "$CFG" 2>/dev/null || echo 'dtparam=spi=on' | sudo tee -a "$CFG" >/dev/null
fi

log "fetching GOST MINI -> $DEST"
# STAGE THEN SWAP. Never delete the working install before the new one is
# proven good: an earlier version rm -rf'd $DEST first, and when a later step
# failed the user was left with no install at all (and a service that could no
# longer chdir into it).
TMP="$(mktemp -d)"
cleanup_tmp() { rm -rf "$TMP"; }
trap cleanup_tmp EXIT
# Plain shallow clone. (A sparse checkout looks tempting, but cone mode only
# accepts DIRECTORIES -- passing backend/dtc_lookup.py made it fatal.)
if ! git clone --depth 1 "$REPO" "$TMP/repo" 2>&1 | tail -2; then
  die_msg="could not download GOST MINI"
  if [ -d "$DEST" ]; then warn "$die_msg -- keeping the existing install"; else
    echo "[gost-mini] $die_msg and nothing is installed" >&2; exit 1; fi
else
  STAGE="$TMP/stage"
  mkdir -p "$STAGE"
  cp -r "$TMP/repo/mini/." "$STAGE/"
  # gostmini.py checks its own directory first, so the shared DTC table lives
  # right beside it (no stray ~/backend folder).
  cp "$TMP/repo/backend/dtc_lookup.py" "$STAGE/dtc_lookup.py"
  # sanity-check the staged copy BEFORE touching what's installed
  if [ -f "$STAGE/gostmini.py" ] && [ -f "$STAGE/display.py" ] && [ -f "$STAGE/dtc_lookup.py" ]; then
    # keep the fetched e-paper driver + saved settings across upgrades
    [ -d "$DEST/waveshare_epd" ] && cp -r "$DEST/waveshare_epd" "$STAGE/waveshare_epd"
    rm -rf "$DEST.old"
    [ -d "$DEST" ] && mv "$DEST" "$DEST.old"
    mkdir -p "$(dirname "$DEST")"
    mv "$STAGE" "$DEST"
    rm -rf "$DEST.old"
    log "installed $(ls "$DEST"/*.py | wc -l) files"
  else
    warn "downloaded copy looks incomplete -- keeping the existing install"
  fi
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

log "allowing the service to re-bind rfcomm after a power cycle"
# When the truck is switched off the adapter loses power and the rfcomm channel
# dies; GOST MINI re-binds it automatically, which needs these two commands.
sudo tee /etc/sudoers.d/gost-mini >/dev/null <<SUDOEOF
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/rfcomm bind *, /usr/bin/rfcomm release *, /usr/bin/systemctl restart gost-obd-link.service
SUDOEOF
sudo chmod 440 /etc/sudoers.d/gost-mini
sudo visudo -c -f /etc/sudoers.d/gost-mini >/dev/null 2>&1 || {
  warn "sudoers snippet invalid -- removing"; sudo rm -f /etc/sudoers.d/gost-mini; }

log "installing the Wi-Fi watchdog (keeps the unit reachable in a vehicle)"
sudo apt-get install -y --no-install-recommends avahi-daemon >/dev/null 2>&1 || true
chmod +x "$DEST/gost-mini-wifi.sh" 2>/dev/null || true
sudo tee /etc/systemd/system/gost-mini-wifi.service >/dev/null <<WIFIEOF
[Unit]
Description=GOST MINI Wi-Fi watchdog (power-save off + auto-reconnect + mDNS)
After=NetworkManager.service network.target
Wants=NetworkManager.service

[Service]
Type=simple
ExecStart=$DEST/gost-mini-wifi.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
WIFIEOF
sudo systemctl daemon-reload
sudo systemctl enable gost-mini-wifi.service >/dev/null 2>&1

log "installing PiSugar battery support"
sudo raspi-config nonint do_i2c 0 2>/dev/null || true      # PiSugar talks I2C
if ! systemctl list-unit-files 2>/dev/null | grep -q pisugar-server; then
  # Official PiSugar power manager (installs pisugar-server on :8423, which
  # gostmini.py reads for the battery %). Best-effort -- no PiSugar just means
  # the battery corner stays blank.
  curl -fsSL http://cdn.pisugar.com/release/pisugar-power-manager.sh -o /tmp/pisugar.sh     && sudo bash /tmp/pisugar.sh -c release >/dev/null 2>&1     && log "pisugar-server installed"     || warn "PiSugar server not installed -- battery %% will be blank"
  rm -f /tmp/pisugar.sh
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
SupplementaryGroups=dialout
WorkingDirectory=$DEST
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u $DEST/gostmini.py
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
