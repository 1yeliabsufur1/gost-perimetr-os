#!/usr/bin/env bash
# Find, pair and bind a Bluetooth OBD-II adapter -- no MAC typing required.
#
#   ./pair-obd.sh            # auto-detect by name (OBDLink/ELM/OBDII/Vgate...)
#   ./pair-obd.sh AA:BB:CC:DD:EE:FF   # or force a specific MAC
#
# The adapter is powered BY THE CAR, so plug it into the OBD port and turn the
# key to accessory first -- otherwise it isn't broadcasting and nothing can
# find it.
set -uo pipefail

PIN="${OBD_PIN:-1234}"          # most ELM clones use 1234 or 0000
SCAN_SECS="${SCAN_SECS:-20}"
MAC="${1:-}"

say() { printf '\n\033[1m[pair-obd]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[pair-obd] %s\033[0m\n' "$*"; exit 1; }

command -v bluetoothctl >/dev/null || die "bluez not installed (run install.sh)"
sudo rfkill unblock bluetooth 2>/dev/null || true
sudo systemctl start bluetooth 2>/dev/null || true
bluetoothctl power on >/dev/null 2>&1

if [ -z "$MAC" ]; then
  say "scanning ${SCAN_SECS}s for an OBD adapter (it must be plugged in + key on)"
  bluetoothctl --timeout "$SCAN_SECS" scan on >/dev/null 2>&1
  # match on the usual adapter names
  MAC="$(bluetoothctl devices 2>/dev/null \
        | grep -iE 'obd|elm|obdlink|vgate|viecar|veepeak|konnwei|panlong' \
        | head -1 | awk '{print $2}')"
  if [ -z "$MAC" ]; then
    echo
    echo "No OBD adapter found. Everything Bluetooth saw:"
    bluetoothctl devices 2>/dev/null | sed 's/^/    /' || echo "    (nothing)"
    echo
    echo "Checklist:"
    echo "  * adapter plugged into the OBD port, key at least in ACCESSORY"
    echo "  * its LED is on (it's powered by the car, not by the Pi)"
    echo "  * if it has a pairing button, press it now and re-run"
    echo "  * some adapters are BLE-only -- those can't do rfcomm serial"
    echo
    echo "If you know the MAC:  ./pair-obd.sh AA:BB:CC:DD:EE:FF"
    exit 1
  fi
  NAME="$(bluetoothctl devices 2>/dev/null | grep -i "$MAC" | cut -d' ' -f3-)"
  say "found: ${NAME:-unknown}  [$MAC]"
fi

say "pairing $MAC (PIN $PIN)"
# agent handles the PIN prompt non-interactively
{
  echo "power on"
  echo "agent on"
  echo "default-agent"
  echo "pair $MAC"
  sleep 4
  echo "$PIN"
  sleep 3
  echo "trust $MAC"
  sleep 2
  echo "quit"
} | bluetoothctl >/dev/null 2>&1

if bluetoothctl info "$MAC" 2>/dev/null | grep -qi "Paired: yes"; then
  say "paired + trusted"
else
  echo "  (pairing may not have completed -- continuing; some adapters bind anyway)"
fi

# Remember the MAC so GOST MINI can re-bind itself after the truck is
# switched off and back on (the rfcomm channel dies with the adapter's power).
echo "$MAC" > "$HOME/.gost-obd-mac"
echo "$MAC" | sudo tee /etc/gost-obd-mac >/dev/null 2>&1 || true

say "binding /dev/rfcomm0"
sudo rfcomm release 0 2>/dev/null || true
sudo rfcomm bind 0 "$MAC" || die "rfcomm bind failed"

# make it survive reboots
UNIT=/etc/systemd/system/gost-obd-bind.service
sudo tee "$UNIT" >/dev/null <<UNITEOF
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

ls -l /dev/rfcomm0 2>/dev/null && say "/dev/rfcomm0 ready" || die "no /dev/rfcomm0"
say "restarting GOST MINI"
sudo systemctl restart gost-mini 2>/dev/null || true
echo
echo "  Done. The panel should link within a few seconds."
echo "  Watch it:  journalctl -u gost-mini -f"
