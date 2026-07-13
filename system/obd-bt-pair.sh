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

echo "Scanning for '$NAME_MATCH' (adapter must be powered -- ignition on)..."
bluetoothctl --timeout 15 scan on >/dev/null 2>&1
MAC="$(bluetoothctl devices 2>/dev/null | awk -v n="$NAME_MATCH" 'index($0,n){print $2; exit}')"
[ -z "$MAC" ] && { echo "MX+ not found. Ignition on? Not connected to a phone?"; exit 1; }

# NoInputNoOutput agent auto-accepts "just works"; some MX+ units want PIN 1234
# -- if this fails, fall back to a bt-agent PIN reply (see spec s10).
bluetoothctl --agent NoInputNoOutput pair "$MAC" >/dev/null 2>&1
bluetoothctl trust "$MAC" >/dev/null 2>&1

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
