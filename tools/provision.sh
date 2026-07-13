#!/usr/bin/env bash
# GOST/PERIMETR OS dependency provisioner.
#
# Installs everything that needs the internet: apt packages, the Python venv,
# pip deps. Hardware-independent -- it does NOT touch users, per-device
# service templating, or media, so it is safe to run at BUILD TIME inside the
# image's ARM64 rootfs (qemu chroot). When it has run, /opt/gost/.provisioned
# exists and the first-boot install.sh skips all of this, making first boot
# fully offline and fast.
#
# Also callable standalone on-device (install.sh invokes it when the marker
# is absent). Idempotent.
set -euo pipefail

GOST_HOME=/opt/gost
log() { echo "[gost-provision] $*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "provision.sh: must run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
log "apt-get update"
apt-get update

log "installing base packages (GUI stack, media, python, tooling)"
apt-get install -y \
  python3 python3-venv python3-pip python3-dev \
  build-essential swig \
  ffmpeg mpv yt-dlp \
  cage seatd \
  network-manager rsync curl git i2c-tools rfkill \
  bluez bluez-tools \
  fonts-dejavu-core fonts-noto-mono

# liblgpio-dev is Pi-OS-only (lgpio wheel builds against -llgpio on py3.13);
# absent on generic Debian -- keep outside set -e so an x86 test build works.
apt-get install -y liblgpio-dev || \
  log "WARNING: liblgpio-dev unavailable (non-Pi) -- GPIO pad disabled, rest works"

# trixie renamed chromium; try both names outside set -e.
apt-get install -y chromium-browser || apt-get install -y chromium || \
  log "WARNING: no chromium package found -- kiosk will not start"

# Enable (not --now: at build time there is no running systemd) so seatd is
# up on the real first boot. install.sh does the --now start on-device.
systemctl enable seatd 2>/dev/null || true

log "creating Python venv + installing pip deps"
mkdir -p "$GOST_HOME"
[ -d "$GOST_HOME/venv" ] || python3 -m venv "$GOST_HOME/venv"
"$GOST_HOME/venv/bin/pip" install --upgrade pip
"$GOST_HOME/venv/bin/pip" install obd websockets gpiozero pyserial

# Trim apt caches so the baked image stays small.
apt-get clean || true

touch "$GOST_HOME/.provisioned"
log "provisioning complete -- image is now offline-installable"
