#!/usr/bin/env bash
# GOST/PERIMETR OS installer. Runs as root, either interactively (manual
# re-run/update) or once from gost-firstboot.service on first boot.
# Idempotent: safe to run again (e.g. to pick up an updated checkout).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh: must run as root (sudo $0)" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOST_HOME=/opt/gost

log() { echo "[gost-install] $*"; }

# ---------------------------------------------------------------------------
# 1. Resolve the operator user. The firstboot service runs with NO
#    SUDO_USER -- never assume it's set. Fall back to the uid-1000 account
#    that Raspberry Pi Imager / cloud-init creates, then to a fresh 'gost'
#    user as a last resort.
# ---------------------------------------------------------------------------
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  GOST_USER="${SUDO_USER}"
elif getent passwd 1000 >/dev/null 2>&1; then
  GOST_USER="$(getent passwd 1000 | cut -d: -f1)"
else
  GOST_USER="gost"
  id -u "$GOST_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$GOST_USER"
fi
log "operator user: $GOST_USER"
echo "$GOST_USER" > /etc/gost-user

for grp in dialout gpio spi i2c video render seat netdev sudo; do
  getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" "$GOST_USER" || true
done

# ---------------------------------------------------------------------------
# 2. System packages.
#    ffmpeg is required for ffprobe (guide durations are real, never assumed).
#    swig/python3-dev/build-essential/liblgpio-dev are required because the
#    lgpio wheel gpiozero depends on builds from source on Python 3.13 --
#    it needs swig AND headers to link against -llgpio.
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update

apt-get install -y \
  python3 python3-venv python3-pip python3-dev \
  build-essential swig \
  ffmpeg mpv yt-dlp \
  cage seatd \
  network-manager rsync curl git i2c-tools \
  fonts-dejavu-core fonts-noto-mono

# liblgpio-dev exists on Raspberry Pi OS (needed so the lgpio wheel that
# gpiozero pulls in can build against -llgpio on Python 3.13) but is NOT in
# generic Debian's archive -- keep it out of the set -e line above so an
# x86 VM install (VirtualBox testing, no GPIO anyway) doesn't abort here.
apt-get install -y liblgpio-dev || \
  log "WARNING: liblgpio-dev unavailable (non-Pi system?) -- GPIO pad will be disabled, everything else works"

# trixie renamed the chromium package; try both names OUTSIDE the main
# set -e apt line above so a naming mismatch can never abort the whole install.
apt-get install -y chromium-browser || apt-get install -y chromium || \
  log "WARNING: no chromium package found under either name -- kiosk will not start"

systemctl enable --now seatd

# ---------------------------------------------------------------------------
# 3. Python venv.
# ---------------------------------------------------------------------------
[ -d "$GOST_HOME/venv" ] || python3 -m venv "$GOST_HOME/venv"
"$GOST_HOME/venv/bin/pip" install --upgrade pip
"$GOST_HOME/venv/bin/pip" install obd websockets gpiozero pyserial

# ---------------------------------------------------------------------------
# 4. Copy project tree into place. Never clobber media/ or state/ that a
#    prior install already populated with the operator's own content.
# ---------------------------------------------------------------------------
mkdir -p "$GOST_HOME"
rsync -a --exclude='media/' --exclude='state/' --exclude='.git/' "$SRC_DIR"/ "$GOST_HOME"/

if [ ! -d "$GOST_HOME/media" ]; then
  mkdir -p "$GOST_HOME/media/TV/COMMERCIALS" "$GOST_HOME/media/MUSIC" "$GOST_HOME/media/PODCASTS"
  [ -d "$SRC_DIR/media" ] && cp -r "$SRC_DIR"/media/. "$GOST_HOME/media/" 2>/dev/null || true
fi
mkdir -p "$GOST_HOME/maps" "$GOST_HOME/state"

chown -R "$GOST_USER":"$GOST_USER" "$GOST_HOME"
chmod +x "$GOST_HOME/install.sh" "$GOST_HOME/system/kiosk-start.sh" \
  "$GOST_HOME/system/gost-setpass" "$GOST_HOME/system/gost-settime" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Privileged helpers (password/clock never touch argv -- piped via stdin)
#    + sudoers whitelist restricted to these exact paths.
# ---------------------------------------------------------------------------
install -m 750 -o root -g root "$GOST_HOME/system/gost-setpass" /usr/local/sbin/gost-setpass
install -m 750 -o root -g root "$GOST_HOME/system/gost-settime" /usr/local/sbin/gost-settime

SUDOERS_TMP="$(mktemp)"
sed "s/__GOST_USER__/$GOST_USER/g" "$GOST_HOME/system/sudoers-gost" > "$SUDOERS_TMP"
visudo -c -f "$SUDOERS_TMP"
install -m 440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/gost
rm -f "$SUDOERS_TMP"

# ---------------------------------------------------------------------------
# 6. systemd units (User= is templated per-install since it depends on the
#    detected operator account).
# ---------------------------------------------------------------------------
for unit in hud-backend.service hud-kiosk.service; do
  sed "s/__GOST_USER__/$GOST_USER/g" "$GOST_HOME/system/$unit" > "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable hud-backend.service
systemctl enable hud-kiosk.service

# ---------------------------------------------------------------------------
# 7. SPI/I2C dtparam for MCP3008 pots / optional hardware, if not already set.
#    (The image-build workflow also does this; kept here too so a stock,
#    non-custom-image install still ends up with working SPI.)
# ---------------------------------------------------------------------------
CONFIG_TXT=/boot/firmware/config.txt
[ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
if [ -f "$CONFIG_TXT" ]; then
  grep -q '^dtparam=spi=on' "$CONFIG_TXT" || echo 'dtparam=spi=on' >> "$CONFIG_TXT"
  grep -q '^dtparam=i2c_arm=on' "$CONFIG_TXT" || echo 'dtparam=i2c_arm=on' >> "$CONFIG_TXT"
fi

# ---------------------------------------------------------------------------
# 8. Done. Mark installed, disable the firstboot unit so it never runs
#    again, and reboot straight into the kiosk.
# ---------------------------------------------------------------------------
touch "$GOST_HOME/.installed"
systemctl disable gost-firstboot.service 2>/dev/null || true

log "install complete -- rebooting into kiosk"
sleep 2
reboot || true
