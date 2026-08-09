#!/usr/bin/env bash
# Find, pair and bind a Bluetooth OBD-II adapter -- no MAC typing required.
#
#   ./pair-obd.sh                     # auto-detect by name
#   ./pair-obd.sh AA:BB:CC:DD:EE:FF   # or force a specific MAC
#
# IMPORTANT: turn the ignition ON first.
# Adapters like the OBDLink MX+ SLEEP to protect the car's battery and stop
# advertising over Bluetooth entirely. Pairing against a sleeping adapter
# fails with "not available" -- so this script waits for it to wake, and keeps
# a scan running throughout to hold it in bluez's cache.
set -uo pipefail

PIN="${OBD_PIN:-1234}"
WAIT_SECS="${WAIT_SECS:-90}"      # how long to wait for the adapter to appear
MAC="${1:-}"
NAME_RE='obd|elm|obdlink|vgate|viecar|veepeak|konnwei|panlong'

say() { printf '\n\033[1m[pair-obd]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[pair-obd] %s\033[0m\n' "$*"; exit 1; }

command -v bluetoothctl >/dev/null || die "bluez not installed (run install.sh)"
sudo rfkill unblock bluetooth 2>/dev/null || true
sudo systemctl start bluetooth 2>/dev/null || true
bluetoothctl power on >/dev/null 2>&1

# Keep scanning in the background for the whole run: a sleeping adapter only
# advertises in short bursts, and bluez forgets devices it can't see.
timeout $((WAIT_SECS + 60)) bluetoothctl --timeout $((WAIT_SECS + 55)) scan on >/dev/null 2>&1 &
SCAN_PID=$!
cleanup() { kill "$SCAN_PID" 2>/dev/null || true; }
trap cleanup EXIT

say "waiting up to ${WAIT_SECS}s for the adapter to advertise"
say "(turn the ignition ON -- a sleeping adapter is invisible to Bluetooth)"
FOUND=""
for _ in $(seq 1 $((WAIT_SECS / 3))); do
  if [ -n "$MAC" ]; then
    bluetoothctl devices 2>/dev/null | grep -qi "$MAC" && { FOUND="$MAC"; break; }
  else
    HIT="$(bluetoothctl devices 2>/dev/null | grep -iE "$NAME_RE" | head -1)"
    if [ -n "$HIT" ]; then
      FOUND="$(echo "$HIT" | awk '{print $2}')"
      say "found: $(echo "$HIT" | cut -d' ' -f3-)  [$FOUND]"
      break
    fi
  fi
  printf '.'
  sleep 3
done
echo

if [ -z "$FOUND" ]; then
  echo
  echo "Adapter never appeared. Everything Bluetooth saw:"
  bluetoothctl devices 2>/dev/null | sed 's/^/    /' || echo "    (nothing)"
  echo
  echo "Checklist:"
  echo "  * IGNITION ON (not just accessory) -- the MX+/ELM sleeps otherwise"
  echo "  * adapter seated firmly in the OBD port, its LED lit"
  echo "  * if it has a button, press it to wake it, then re-run"
  exit 1
fi
MAC="$FOUND"

# Clear any half-pairing -- a previous failed attempt blocks a clean retry.
if bluetoothctl info "$MAC" 2>/dev/null | grep -qi "Paired: no"; then
  bluetoothctl remove "$MAC" >/dev/null 2>&1
  sleep 2
fi

pair_try() {   # $1 = agent capability
  { echo "agent $1"; sleep 1
    echo "default-agent";  sleep 1
    echo "pair $MAC";      sleep 12
    echo "$PIN";           sleep 5
    echo "yes";            sleep 3
    echo "quit"; } | timeout 45 bluetoothctl >/dev/null 2>&1
  bluetoothctl info "$MAC" 2>/dev/null | grep -qi "Paired: yes"
}

say "pairing $MAC"
# "just works" first (most modern adapters), then PIN-based as a fallback
if pair_try NoInputNoOutput; then say "paired (just-works)"
elif pair_try KeyboardOnly;   then say "paired (PIN $PIN)"
else
  bluetoothctl info "$MAC" 2>/dev/null | grep -iE "Paired|Connected" | sed 's/^/  /'
  die "pairing failed -- is the ignition on? try: OBD_PIN=0000 ./pair-obd.sh"
fi
bluetoothctl trust "$MAC" >/dev/null 2>&1

# Remember the MAC so GOST MINI can re-bind itself after a power cycle.
echo "$MAC" > "$HOME/.gost-obd-mac"
echo "$MAC" | sudo tee /etc/gost-obd-mac >/dev/null 2>&1 || true

say "binding /dev/rfcomm0"
sudo rfcomm release 0 2>/dev/null || true
sudo rfcomm bind 0 "$MAC" || die "rfcomm bind failed"

# make it survive reboots
sudo tee /etc/systemd/system/gost-obd-bind.service >/dev/null <<UNITEOF
[Unit]
Description=Bind the Bluetooth OBD adapter to /dev/rfcomm0
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/rfcomm bind 0 $MAC
ExecStop=/usr/bin/rfcomm release 0

[Install]
WantedBy=multi-user.target
UNITEOF
sudo systemctl daemon-reload
sudo systemctl enable gost-obd-bind.service >/dev/null 2>&1

ls -l /dev/rfcomm0 >/dev/null 2>&1 || die "no /dev/rfcomm0"
say "verifying the ECU actually answers"
sudo systemctl stop gost-mini 2>/dev/null || true
sleep 1
python3 - <<'PY'
import sys, os
sys.path.insert(0, os.path.expanduser("~/gost-mini"))
try:
    from obd import MiniOBD
except Exception as e:
    print("  (could not import reader: %s)" % e); raise SystemExit
o = MiniOBD()
print("  connected:", o.connect(), "|", o.detail)
o.close()
PY
sudo systemctl start gost-mini 2>/dev/null || true

echo
echo "  Done. Watch it:  journalctl -u gost-mini -f"
