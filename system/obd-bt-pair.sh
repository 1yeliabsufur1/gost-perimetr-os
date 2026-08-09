#!/usr/bin/env bash
# One-time headless pair+trust of the OBDLink MX+ (Part 1). Idempotent.
# Run in the vehicle with the adapter powered (ignition on):  sudo obd-bt-pair.sh
# Writes MAC + SPP channel to /etc/gost/obd-bt.conf, which the rfcomm link
# service reads. 'trust' is non-optional: without it BlueZ won't auto-authorise
# headless reconnects on every ignition cycle.
set -u
CONF=/etc/gost/obd-bt.conf
NAME_MATCH="OBDLink"          # MX+ advertises as "OBDLink MX+"

rfkill unblock bluetooth 2>/dev/null || true
bluetoothctl power on >/dev/null 2>&1

# already provisioned + trusted?
if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
  if [ -n "${OBD_BT_MAC:-}" ] && bluetoothctl info "$OBD_BT_MAC" 2>/dev/null | grep -q "Trusted: yes"; then
    echo "Already paired+trusted: $OBD_BT_MAC"; exit 0
  fi
fi

# The MX+ SLEEPS to protect the battery and only advertises in short bursts,
# so a single scan-then-pair loses the race (bailey, repeatedly): it would be
# found, then "not available" a second later. Hold a scan open and pair the
# INSTANT it appears. Also clear any stale bond first -- reflashing the Pi
# gives it a new Bluetooth identity, so the adapter's old key no longer
# matches and the SPP channel is refused with "Permission denied".
WAIT_SECS="${WAIT_SECS:-120}"
echo "Waiting up to ${WAIT_SECS}s for '$NAME_MATCH' (ignition ON; press its button)..."
command -v bt-agent >/dev/null 2>&1 && { pkill -f bt-agent 2>/dev/null; bt-agent -c NoInputNoOutput -d 2>/dev/null & sleep 1; }
timeout $((WAIT_SECS + 20)) bluetoothctl --timeout $((WAIT_SECS + 15)) scan on >/dev/null 2>&1 &
SCAN_PID=$!
trap 'kill "$SCAN_PID" 2>/dev/null' EXIT

MAC=""
for _ in $(seq 1 $((WAIT_SECS / 2))); do
  MAC="$(bluetoothctl devices 2>/dev/null | awk -v n="$NAME_MATCH" 'index($0,n){print $2; exit}')"
  if [ -n "$MAC" ]; then
    # pair immediately -- it may be asleep again within seconds
    if bluetoothctl info "$MAC" 2>/dev/null | grep -qi "Paired: no"; then
      bluetoothctl remove "$MAC" >/dev/null 2>&1; sleep 1
    fi
    bluetoothctl --agent NoInputNoOutput pair "$MAC" >/dev/null 2>&1
    bluetoothctl info "$MAC" 2>/dev/null | grep -qi "Paired: yes" && break
  fi
  sleep 2
done
[ -z "$MAC" ] && { echo "MX+ not found. Ignition on? Not connected to a phone?"; exit 1; }
bluetoothctl trust "$MAC" >/dev/null 2>&1
bluetoothctl info "$MAC" 2>/dev/null | grep -qi "Paired: yes"   || echo "NOTE: not bonded -- continuing; some adapters serve SPP anyway"

CH="$(sdptool browse "$MAC" 2>/dev/null | awk '/Serial Port/{sp=1} sp&&/Channel:/{print $2; exit}')"
CH="${CH:-1}"

install -d -m0755 /etc/gost
cat > "$CONF" <<EOF
OBD_BT_MAC=$MAC
OBD_BT_CHANNEL=$CH
OBD_RFCOMM_NODE=/dev/rfcomm0
EOF
echo "Provisioned $MAC ch$CH -> $CONF"
echo "Now: sudo systemctl restart obd-rfcomm  (or reboot)"
