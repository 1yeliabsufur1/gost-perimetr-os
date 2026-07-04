#!/usr/bin/env bash
# Launches the Wayland kiosk compositor (cage) with Chromium pinned to the
# GOST frontend. Waits FOREVER for the backend HTTP port -- never bail out,
# since first-boot hardware bring-up (OBD adapter, GPS, etc.) can be slow and
# a bailed-out kiosk just dumps the operator onto a bare tty1.
#
# NOTHING here may fail silently: every wait/error state prints to the
# console, so the screen always says what's happening instead of sitting
# black while systemd restart-loops us.
set -uo pipefail

say() {
  echo "GOST: $*"
  echo "GOST: $*" > /dev/tty1 2>/dev/null || true
}

say "kiosk starting -- waiting for backend on 127.0.0.1:8766 ..."
tries=0
until curl -fs -o /dev/null "http://127.0.0.1:8766/index.html" 2>/dev/null; do
  sleep 1
  tries=$((tries + 1))
  if [ $((tries % 15)) -eq 0 ]; then
    say "still waiting for backend (${tries}s) -- check: journalctl -u hud-backend"
  fi
done
say "backend is up -- starting display"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
export WLR_SEAT="${WLR_SEAT:-seat0}"
export WLR_LIBINPUT_NO_DEVICES=1

CHROMIUM_BIN="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$CHROMIUM_BIN" ]; then
  say "ERROR: no chromium binary found -- run: sudo /opt/gost-src/install.sh"
  sleep 10   # slow the systemd restart loop so the message stays readable
  exit 1
fi
if ! command -v cage >/dev/null; then
  say "ERROR: cage compositor not installed -- run: sudo /opt/gost-src/install.sh"
  sleep 10
  exit 1
fi

exec cage -- "$CHROMIUM_BIN" \
  --kiosk \
  --app=http://127.0.0.1:8766/index.html \
  --disable-restore-session-state \
  --disable-session-crashed-bubble \
  --noerrdialogs \
  --disable-infobars \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --ozone-platform=wayland
