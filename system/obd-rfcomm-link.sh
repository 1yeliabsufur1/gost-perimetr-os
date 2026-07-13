#!/usr/bin/env bash
# Supervised SPP link to the OBDLink MX+ (Part 1). Blocks while connected;
# exits when the link drops (engine off / out of range). systemd
# (Restart=always) re-invokes this to reconnect. Owns /dev/rfcomm0.
#
# Self-provisioning: if the adapter was never paired, tries obd-bt-pair.sh
# once, and if there's still no config it backs off slowly (so a bench Pi
# with no adapter doesn't spin the restart limiter) instead of failing hard.
set -u
CONF=/etc/gost/obd-bt.conf

if [ ! -f "$CONF" ]; then
  /usr/local/bin/obd-bt-pair.sh >/dev/null 2>&1 || true
  if [ ! -f "$CONF" ]; then
    echo "obd-rfcomm: not paired yet -- run 'sudo obd-bt-pair.sh' in the truck."
    sleep 30   # slow retry; no adapter present is a normal bench state
    exit 0
  fi
fi

# shellcheck disable=SC1090
. "$CONF"
: "${OBD_BT_MAC:?OBD_BT_MAC not set in $CONF}"
: "${OBD_BT_CHANNEL:=auto}"
: "${OBD_RFCOMM_NODE:=/dev/rfcomm0}"
NODE_NUM="${OBD_RFCOMM_NODE##*rfcomm}"   # -> 0 from /dev/rfcomm0
CTRL_WAIT="${OBD_CTRL_WAIT:-15}"

rfkill unblock bluetooth 2>/dev/null || true

# wait for the controller to be powered (AutoEnable should do this; guard anyway)
for _ in $(seq "$CTRL_WAIT"); do
  bluetoothctl show 2>/dev/null | grep -q "Powered: yes" && break
  sleep 1
done

# resolve SPP channel if not pinned (sdptool may be absent -> default 1)
if [ "$OBD_BT_CHANNEL" = "auto" ] || [ -z "$OBD_BT_CHANNEL" ]; then
  OBD_BT_CHANNEL="$(sdptool browse "$OBD_BT_MAC" 2>/dev/null \
    | awk '/Serial Port/{sp=1} sp&&/Channel:/{print $2; exit}')"
  OBD_BT_CHANNEL="${OBD_BT_CHANNEL:-1}"
fi

# clear any stale binding on this node ("address already in use" after an
# unclean drop / ignition yank)
rfcomm release "$NODE_NUM" 2>/dev/null || true

echo "obd-rfcomm: connecting $OBD_BT_MAC ch$OBD_BT_CHANNEL -> $OBD_RFCOMM_NODE"
# blocking connect; returns (non-zero) when the link drops -> systemd restarts us
exec rfcomm connect "$NODE_NUM" "$OBD_BT_MAC" "$OBD_BT_CHANNEL"
