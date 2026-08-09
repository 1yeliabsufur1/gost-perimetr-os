#!/usr/bin/env bash
# GOST MINI Wi-Fi watchdog.
#
# The Zero 2 W's radio re-enables power-save on its own and NetworkManager
# doesn't always re-associate after a drop, so the unit vanishes from the
# network -- painful when it's sitting in a vehicle and you want to SSH in.
# Every 30s: force power-save off, reconnect if the link is down, keep mDNS
# alive so gostmini.local keeps resolving.
#
# Battery-aware: this runs on a PiSugar, so it does nothing expensive while
# the link is healthy.
set -uo pipefail
IFACE="${GOST_WIFI_IFACE:-wlan0}"
log() { echo "$(date '+%F %T') [wifi-wd] $*"; }

while true; do
  iw dev "$IFACE" set power_save off 2>/dev/null || true

  # Is the interface actually associated?
  state=""
  if command -v nmcli >/dev/null 2>&1; then
    state="$(nmcli -t -f DEVICE,STATE dev 2>/dev/null | awk -F: -v i="$IFACE" '$1==i{print $2}')"
    if [ "$state" != "connected" ]; then
      log "link is '${state:-unknown}' -- reconnecting"
      rfkill unblock wifi 2>/dev/null || true
      nmcli radio wifi on 2>/dev/null || true
      conn="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2 ~ /wireless/{print $1; exit}')"
      if [ -n "$conn" ]; then
        nmcli connection up "$conn" 2>/dev/null || nmcli device connect "$IFACE" 2>/dev/null || true
      else
        nmcli device connect "$IFACE" 2>/dev/null || true
      fi
    fi
  else
    # Raspberry Pi OS Lite images that still use wpa_supplicant/dhcpcd
    if ! iw dev "$IFACE" link 2>/dev/null | grep -qi "Connected to"; then
      log "not associated -- kicking wpa_supplicant"
      rfkill unblock wifi 2>/dev/null || true
      wpa_cli -i "$IFACE" reconfigure >/dev/null 2>&1 || true
      ip link set "$IFACE" up 2>/dev/null || true
    fi
  fi

  # mDNS must stay up or gostmini.local stops resolving even while online
  systemctl is-active --quiet avahi-daemon 2>/dev/null || \
    systemctl restart avahi-daemon 2>/dev/null || true

  sleep 30
done
