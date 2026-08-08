#!/usr/bin/env bash
# GOST Wi-Fi watchdog. The Pi 5's onboard radio quietly re-enables power-save
# and NetworkManager doesn't always re-associate after a drop, so the truck
# falls off the network mid-session (bailey, repeatedly). This runs as root
# from a systemd service and, every 20s:
#   1. forces radio power-save OFF (the #1 cause of idle drops)
#   2. reconnects the saved Wi-Fi if the link is down
#   3. keeps avahi (mDNS) up so gost.local always resolves
set -uo pipefail
log() { echo "$(date '+%F %T') [wifi-wd] $*"; }

IFACE="${GOST_WIFI_IFACE:-wlan0}"

while true; do
  # 1. power-save OFF (re-assert -- it reverts on reconnect)
  iw dev "$IFACE" set power_save off 2>/dev/null || true

  # 2. reconnect if the interface isn't connected
  state="$(nmcli -t -f DEVICE,STATE dev 2>/dev/null | awk -F: -v i="$IFACE" '$1==i{print $2}')"
  if [ "$state" != "connected" ]; then
    log "link is '$state' -- reconnecting"
    rfkill unblock wifi 2>/dev/null || true
    nmcli radio wifi on 2>/dev/null || true
    conn="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2 ~ /wireless/{print $1; exit}')"
    if [ -n "$conn" ]; then
      nmcli connection up "$conn" 2>/dev/null || nmcli device connect "$IFACE" 2>/dev/null || true
    else
      nmcli device connect "$IFACE" 2>/dev/null || true
    fi
  fi

  # 3. mDNS must stay up or gost.local stops resolving even while online
  if command -v systemctl >/dev/null 2>&1; then
    systemctl is-active --quiet avahi-daemon 2>/dev/null || systemctl restart avahi-daemon 2>/dev/null || true
  fi

  sleep 20
done
