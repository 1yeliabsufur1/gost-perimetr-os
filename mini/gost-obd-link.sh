#!/usr/bin/env bash
# Supervised SPP link to the Bluetooth OBD adapter for GOST MINI.
#
# This mirrors the head unit's proven obd-rfcomm-link.sh. The important bit:
# it uses `rfcomm connect`, NOT `rfcomm bind`.
#   * bind  = create a node that tries to connect when something opens it.
#             Leaves /dev/rfcomm0 in a "bound but not connected" state that
#             never heals -- the exact failure GOST MINI hit.
#   * connect = actively establish the link and HOLD it, blocking until it
#             drops. systemd (Restart=always) then reconnects, which is what
#             makes an ignition cycle recover by itself.
set -u
CONF=/etc/gost-mini/obd-bt.conf

if [ ! -f "$CONF" ]; then
  echo "obd-link: not paired yet -- run ./pair-obd.sh in the vehicle (ignition on)."
  sleep 30            # slow retry; unpaired is a normal bench state
  exit 0
fi
# shellcheck disable=SC1090
. "$CONF"
: "${OBD_BT_MAC:?OBD_BT_MAC not set in $CONF}"
: "${OBD_BT_CHANNEL:=auto}"
: "${OBD_RFCOMM_NODE:=/dev/rfcomm0}"
NODE_NUM="${OBD_RFCOMM_NODE##*rfcomm}"

rfkill unblock bluetooth 2>/dev/null || true

# wait for the controller to come up
for _ in $(seq 15); do
  bluetoothctl show 2>/dev/null | grep -q "Powered: yes" && break
  sleep 1
done

# resolve the SPP channel if it isn't pinned (hardcoding 1 is a common bug --
# the adapter may well advertise Serial Port on another channel)
if [ "$OBD_BT_CHANNEL" = "auto" ] || [ -z "$OBD_BT_CHANNEL" ]; then
  OBD_BT_CHANNEL="$(sdptool browse "$OBD_BT_MAC" 2>/dev/null \
    | awk '/Serial Port/{sp=1} sp&&/Channel:/{print $2; exit}')"
  OBD_BT_CHANNEL="${OBD_BT_CHANNEL:-1}"
fi

# clear a stale binding ("address already in use" after an unclean drop)
rfcomm release "$NODE_NUM" 2>/dev/null || true

echo "obd-link: connecting $OBD_BT_MAC ch$OBD_BT_CHANNEL -> $OBD_RFCOMM_NODE"
# blocking; returns when the link drops -> systemd restarts us
exec rfcomm connect "$NODE_NUM" "$OBD_BT_MAC" "$OBD_BT_CHANNEL"
