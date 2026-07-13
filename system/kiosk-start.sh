#!/usr/bin/env bash
#
# GOST.OS Kiosk Launcher v2 -- hardened for Raspberry Pi appliance deployment.
# Features: infinite backend wait, crash-loop detection, safe mode, full
# logging (file + journal), runtime-dir validation, clean shutdown handling,
# chromium/cage verification, visible status on tty1, restart-safe design.
#
# Exit 42 (safe mode / crash-loop) is honoured by the unit's
# RestartPreventExitStatus so systemd does NOT restart us -- tty2..tty6 stay
# usable via Ctrl+Alt+F2.
set -Eeuo pipefail

STATE_DIR="/opt/gost/state"
LOG="$STATE_DIR/kiosk.log"
START_FILE="$STATE_DIR/kiosk_starts"
mkdir -p "$STATE_DIR"

# Full logging: everything to the persistent log AND to stdout (journal).
exec > >(tee -a "$LOG") 2>&1

say() {
  local msg="[GOST] $*"
  echo "$(date '+%F %T') $msg"
  if [ -w /dev/tty1 ]; then
    printf "\n%s\n" "$msg" > /dev/tty1 2>/dev/null || true
  fi
}

cleanup() {
  say "shutdown requested"
  pkill -f chromium 2>/dev/null || true
  exit 0
}
trap cleanup SIGINT SIGTERM

# ---------------------------- SAFE MODE ------------------------------------
if [ -e /boot/gost_safe_mode ] || [ -e /boot/firmware/gost_safe_mode ]; then
  say "SAFE MODE ACTIVE -- kiosk disabled. Use Ctrl+Alt+F2 for a console."
  exit 42
fi

# ------------------------ CRASH-LOOP PROTECTION ----------------------------
NOW=$(date +%s)
echo "$NOW" >> "$START_FILE"
awk -v now="$NOW" '($1 > now-120)' "$START_FILE" > "${START_FILE}.tmp" 2>/dev/null || true
mv "${START_FILE}.tmp" "$START_FILE" 2>/dev/null || true
RECENT=$(wc -l < "$START_FILE" 2>/dev/null || echo 0)
if [ "${RECENT:-0}" -gt 6 ]; then
  say "CRASH LOOP DETECTED ($RECENT launches in 120s) -- stopping."
  say "Recovery: Ctrl+Alt+F2  |  sudo systemctl status hud-kiosk  |  touch /boot/gost_safe_mode"
  : > "$START_FILE"
  sleep 10
  exit 42
fi

# --------------------------- RUNTIME SETUP ---------------------------------
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
export WLR_SEAT="${WLR_SEAT:-seat0}"
export WLR_LIBINPUT_NO_DEVICES=1

# ---------------------------- DEPENDENCIES ---------------------------------
CHROMIUM_BIN="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$CHROMIUM_BIN" ]; then
  say "Chromium missing -- run: sudo /opt/gost-src/install.sh"
  sleep 15
  exit 1
fi
if ! command -v cage >/dev/null; then
  say "Cage missing -- run: sudo /opt/gost-src/install.sh"
  sleep 15
  exit 1
fi

# -------------------------- WAIT FOR BACKEND -------------------------------
say "waiting for backend on 127.0.0.1:8766 ..."
SECONDS_WAIT=0
while ! curl -fs -o /dev/null http://127.0.0.1:8766/index.html 2>/dev/null; do
  sleep 1
  SECONDS_WAIT=$((SECONDS_WAIT + 1))
  if [ $((SECONDS_WAIT % 15)) -eq 0 ]; then
    say "backend still starting (${SECONDS_WAIT}s) -- check: journalctl -u hud-backend -e"
  fi
done
say "backend online"

# --------------------- PREVENT DUPLICATE INSTANCES -------------------------
if pgrep -f chromium >/dev/null 2>&1; then
  say "chromium already running -- clearing before launch"
  pkill -f chromium || true
  sleep 2
fi

# ----------------------------- START KIOSK ---------------------------------
say "launching GOST UI (cage + $CHROMIUM_BIN)"
exec cage -- "$CHROMIUM_BIN" \
  --kiosk \
  --app=http://127.0.0.1:8766/index.html \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --disable-restore-session-state \
  --disable-features=TranslateUI \
  --noerrdialogs \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --enable-gpu \
  --enable-zero-copy \
  --ozone-platform=wayland \
  --password-store=basic \
  --disable-save-password-bubble
