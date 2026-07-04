#!/usr/bin/env bash
# Launches the Wayland kiosk compositor (cage) with Chromium pinned to the
# GOST frontend. Waits FOREVER for the backend HTTP port -- never bail out,
# since first-boot hardware bring-up (OBD adapter, GPS, etc.) can be slow and
# a bailed-out kiosk just dumps the operator onto a bare tty1.
set -uo pipefail

until curl -fs -o /dev/null "http://127.0.0.1:8766/index.html" 2>/dev/null; do
  sleep 1
done

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
export WLR_SEAT="${WLR_SEAT:-seat0}"
export WLR_LIBINPUT_NO_DEVICES=1

CHROMIUM_BIN="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$CHROMIUM_BIN" ]; then
  echo "kiosk-start.sh: no chromium binary found (chromium-browser/chromium)" >&2
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
