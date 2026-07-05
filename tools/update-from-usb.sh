#!/usr/bin/env bash
# In-place update WITHOUT reflashing. Copy the changed project files onto a
# FAT/exFAT USB stick (either the whole repo, or just a folder named
# "gost-update" containing app/ and backend/ and system/), plug it into the
# Pi, then run:  sudo /opt/gost/tools/update-from-usb.sh
#
# It finds the stick, refreshes /opt/gost + /opt/gost-src, and restarts the
# services. Media/state are never touched. ~10 seconds, no reflash.
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi
log(){ echo "[gost-update] $*"; }

# Find a source dir on any mounted removable media that contains our tree.
SRC=""
for base in /media/*/* /media/* /mnt/* /run/media/*/*; do
  [ -d "$base" ] || continue
  for cand in "$base/gost-update" "$base"; do
    if [ -f "$cand/backend/hud_server.py" ] && [ -f "$cand/app/index.html" ]; then
      SRC="$cand"; break 2
    fi
  done
done

if [ -z "$SRC" ]; then
  log "No update source found. Put app/ backend/ system/ (or a 'gost-update'"
  log "folder containing them) on a USB stick and plug it in, then re-run."
  log "Mounted media seen: $(ls -d /media/*/* /media/* /mnt/* 2>/dev/null | tr '\n' ' ')"
  exit 1
fi
log "using update source: $SRC"

GOST_USER="$(cat /etc/gost-user 2>/dev/null || echo gost)"

# Refresh both the live install and the -src copy (so install.sh stays current).
for dst in /opt/gost /opt/gost-src; do
  for part in app backend system tools; do
    if [ -d "$SRC/$part" ]; then
      mkdir -p "$dst/$part"
      cp -a "$SRC/$part/." "$dst/$part/"
    fi
  done
done
chown -R "$GOST_USER":"$GOST_USER" /opt/gost 2>/dev/null || true
chmod +x /opt/gost/system/*.sh /opt/gost/system/gost-* /opt/gost/tools/*.sh 2>/dev/null || true

# Re-install helpers/units in case they changed.
for h in gost-setpass gost-settime gost-power; do
  [ -f "/opt/gost/system/$h" ] && install -m 750 -o root -g root "/opt/gost/system/$h" "/usr/local/sbin/$h"
done
if [ -f /opt/gost/system/hud-backend.service ]; then
  for unit in hud-backend.service hud-kiosk.service; do
    sed "s/__GOST_USER__/$GOST_USER/g" "/opt/gost/system/$unit" > "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
fi

log "restarting services..."
systemctl restart hud-backend
systemctl restart hud-kiosk
log "done -- the dashboard will reload with the update."
