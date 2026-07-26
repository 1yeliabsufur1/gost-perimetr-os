#!/usr/bin/env bash
# Launch a GameCube/Wii game in Dolphin, fullscreen, on the kiosk's seat.
#
# Started by gost-game.service (Conflicts=+After=hud-kiosk so the dashboard
# stops first and this takes the screen; the service's ExecStopPost restores
# the dashboard when Dolphin exits). Game path is in state/game-target.
set -uo pipefail

STATE_DIR="/opt/gost/state"
LOG="$STATE_DIR/game.log"
mkdir -p "$STATE_DIR"
exec >>"$LOG" 2>&1                     # capture everything (cage/dolphin too)
echo "=== $(date '+%F %T') gost-native-game start (user=$(id -un)) ==="

GAME="$(cat "$STATE_DIR/game-target" 2>/dev/null || true)"
export PATH="/usr/games:/usr/local/bin:/usr/bin:/bin:$PATH"   # dolphin-emu lives in /usr/games
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
export WLR_SEAT="${WLR_SEAT:-seat0}"
# NOTE: unlike the kiosk we do NOT set WLR_LIBINPUT_NO_DEVICES -- Dolphin needs
# the keyboard/gamepad/touchscreen for gameplay and to dismiss dialogs.
# Dolphin is a Qt6 app. Under cage (pure Wayland) it must use the Qt Wayland
# platform plugin -- otherwise Qt falls back to X11 (no X server here) and the
# video backend fails to initialise (bailey: "failed to initialise video
# backend"). Force Wayland + a windowed decoration-free surface.
export QT_QPA_PLATFORM=wayland
export QT_WAYLAND_DISABLE_WINDOWDECORATION=1
echo "GAME=$GAME  XDG=$XDG_RUNTIME_DIR  QT_QPA_PLATFORM=$QT_QPA_PLATFORM"

[ -n "$GAME" ] && [ -f "$GAME" ] || { echo "no game file: '${GAME:-}'"; sleep 2; exit 1; }
command -v dolphin-emu >/dev/null 2>&1 || { echo "dolphin-emu not installed"; sleep 3; exit 1; }
command -v cage >/dev/null 2>&1 || { echo "cage missing"; sleep 3; exit 1; }

# Belt-and-suspenders: make sure the kiosk's chromium + cage have fully released
# the DRM master / seat before we start our own cage, or it dies instantly.
for i in $(seq 1 30); do
  if ! pgrep -x chromium >/dev/null 2>&1 && ! pgrep -x cage >/dev/null 2>&1; then break; fi
  echo "waiting for kiosk to release the display ($i)..."; sleep 0.5
done

# Seed sane Dolphin defaults on first run: OGL backend + native internal res
# (the Pi wants every cycle), fullscreen, and no "confirm stop" dialog so a
# single Esc / controller Home returns straight to the dashboard.
DCFG="${HOME:-/home/gost}/.config/dolphin-emu"
mkdir -p "$DCFG"
[ -f "$DCFG/GFX.ini" ] || cat > "$DCFG/GFX.ini" <<'INI'
[Settings]
Backend = Vulkan
InternalResolution = 1
[Hardware]
VSync = True
INI
[ -f "$DCFG/Dolphin.ini" ] || cat > "$DCFG/Dolphin.ini" <<'INI'
[General]
SkipNKitWarning = True
[Display]
Fullscreen = True
RenderToMain = True
[Interface]
ConfirmStop = False
PauseOnFocusLost = False
INI

echo "launching Dolphin (Esc / Alt+F4 to return to GOST): $(basename "$GAME")"
# -b batch (no GUI, quit when emulation stops), -v Vulkan (V3DV; OGL can't init
# under cage), -e exec the game. cage gives it the Wayland display fullscreen.
cage -- dolphin-emu -b -v Vulkan -e "$GAME"
echo "=== $(date '+%F %T') dolphin exited rc=$? ==="
