#!/usr/bin/env bash
# Launches the Wayland kiosk compositor (cage) with Chromium pinned to the
# GOST frontend. Waits FOREVER for the backend HTTP port -- never bail out on
# a slow backend, since first-boot hardware bring-up can be slow and a bailed
# kiosk just dumps the operator onto a bare tty1.
#
# NOTHING here fails silently: every state prints to tty1. A crash-loop
# breaker gives up after too many rapid restarts (exit 42, which the unit's
# RestartPreventExitStatus honours) so the screen shows help instead of
# flashing forever -- tty2..tty6 stay usable via Ctrl+Alt+F2.
set -uo pipefail

LOG=/opt/gost/state/kiosk.log
mkdir -p /opt/gost/state 2>/dev/null || true

say() {
  local m="GOST: $*"
  echo "$m"
  echo "$m" > /dev/tty1 2>/dev/null || true
  echo "$(date '+%H:%M:%S') $m" >> "$LOG" 2>/dev/null || true
}

# ---- safe-mode short-circuit (belt-and-suspenders; the unit Condition also
#      skips us, but if someone starts the service by hand, honour it too) ----
if [ -e /boot/gost_safe_mode ] || [ -e /boot/firmware/gost_safe_mode ]; then
  say "SAFE MODE marker present -- not starting kiosk. Use the console (Ctrl+Alt+F2)."
  exit 42
fi

# ---- crash-loop breaker: >6 starts within 120s means something is wrong ----
CL=/opt/gost/state/kiosk_starts
now=$(date +%s)
echo "$now" >> "$CL" 2>/dev/null || true
# keep only timestamps from the last 120s
if [ -f "$CL" ]; then
  awk -v c="$now" '($1 > c-120)' "$CL" > "$CL.tmp" 2>/dev/null && mv "$CL.tmp" "$CL" 2>/dev/null || true
  recent=$(wc -l < "$CL" 2>/dev/null | tr -d ' ')
  if [ "${recent:-0}" -gt 6 ]; then
    say "KIOSK CRASH LOOP DETECTED (${recent} starts/120s) -- stopping."
    say "Press Ctrl+Alt+F2 for a console.  Retry: sudo systemctl start hud-kiosk"
    say "Safe boot: touch /boot/gost_safe_mode && sudo reboot"
    : > "$CL" 2>/dev/null || true
    sleep 5
    exit 42   # RestartPreventExitStatus -> systemd will NOT restart us
  fi
fi

say "kiosk starting -- waiting for backend on 127.0.0.1:8766 ..."
tries=0
until curl -fs -o /dev/null "http://127.0.0.1:8766/index.html" 2>/dev/null; do
  sleep 1
  tries=$((tries + 1))
  if [ $((tries % 15)) -eq 0 ]; then
    say "still waiting for backend (${tries}s) -- check: journalctl -u hud-backend -e"
  fi
done
say "backend is up -- starting display"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
export WLR_SEAT="${WLR_SEAT:-seat0}"
export WLR_LIBINPUT_NO_DEVICES=1

CHROMIUM_BIN="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$CHROMIUM_BIN" ]; then
  say "ERROR: no chromium binary -- run: sudo /opt/gost-src/install.sh"
  sleep 10
  exit 1
fi
if ! command -v cage >/dev/null; then
  say "ERROR: cage compositor missing -- run: sudo /opt/gost-src/install.sh"
  sleep 10
  exit 1
fi

say "launching cage + chromium ($CHROMIUM_BIN)"
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
