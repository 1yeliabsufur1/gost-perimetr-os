#!/bin/bash
# Build a turnkey GOST MINI image for the Raspberry Pi Zero 2 W.
#
# No ARM chroot (the builder is Alpine/musl, so qemu-aarch64 can't run inside a
# Debian rootfs). Instead we use the pattern this project already proves on the
# Pi 5: bake the code, services and board config into the image, and let a
# FIRST-BOOT unit do the apt install + driver fetch on the device itself.
#
# Result: flash it, set Wi-Fi/SSH in Raspberry Pi Imager, boot once, and the
# panel comes up on its own. Only the one-time OBD pairing is left.
set -Eeuo pipefail

WORK=/root/minibuild
SRC=/mnt/z/projectGOST
OUT=/mnt/z/projectGOST/dist
IMG="$WORK/gost-mini.img"
GROW_MB=600

log(){ printf '\n[mini-img] %s\n' "$*"; }
die(){ printf '[mini-img] ERROR: %s\n' "$*" >&2; exit 1; }

cd "$WORK"
[ -s raspios.img.xz ] || die "raspios.img.xz missing/empty -- download it first"

log "decompressing base image"
rm -f "$IMG"
xz -dc raspios.img.xz > "$IMG"

log "growing rootfs by ${GROW_MB}MB"
truncate -s +${GROW_MB}M "$IMG"
LOOP=$(losetup -fP --show "$IMG"); sleep 2
parted -s "$LOOP" resizepart 2 100% || true
e2fsck -fy "${LOOP}p2" >/dev/null 2>&1 || true
resize2fs "${LOOP}p2" >/dev/null 2>&1 || true
losetup -d "$LOOP"; sleep 1

log "mounting"
LOOP=$(losetup -fP --show "$IMG"); sleep 2
ROOT="$WORK/root"; mkdir -p "$ROOT"
mount "${LOOP}p2" "$ROOT"
BOOT="$ROOT/boot/firmware"; [ -d "$BOOT" ] || BOOT="$ROOT/boot"
mount "${LOOP}p1" "$BOOT"
cleanup(){ umount "$BOOT" 2>/dev/null||true; umount "$ROOT" 2>/dev/null||true; losetup -d "$LOOP" 2>/dev/null||true; }
trap cleanup EXIT

log "staging GOST MINI -> /opt/gost-mini"
rm -rf "$ROOT/opt/gost-mini"; mkdir -p "$ROOT/opt/gost-mini"
cp "$SRC"/mini/*.py "$ROOT/opt/gost-mini/"
cp "$SRC"/mini/*.sh "$ROOT/opt/gost-mini/"
cp "$SRC"/backend/dtc_lookup.py "$ROOT/opt/gost-mini/"
chmod +x "$ROOT/opt/gost-mini"/*.sh

log "board config: SPI (e-paper HAT) + I2C (PiSugar)"
CFG="$BOOT/config.txt"
grep -q '^dtparam=spi=on'     "$CFG" 2>/dev/null || echo 'dtparam=spi=on'     >> "$CFG"
grep -q '^dtparam=i2c_arm=on' "$CFG" 2>/dev/null || echo 'dtparam=i2c_arm=on' >> "$CFG"

log "SSH on + deploy key"
touch "$BOOT/ssh"
KEY="$(cat /mnt/c/wsl-gostbuild/deploy-key.pub 2>/dev/null || true)"
if [ -n "$KEY" ]; then
  mkdir -p "$ROOT/etc/skel/.ssh"
  echo "$KEY" > "$ROOT/etc/skel/.ssh/authorized_keys"
  chmod 700 "$ROOT/etc/skel/.ssh"; chmod 600 "$ROOT/etc/skel/.ssh/authorized_keys"
  echo "$KEY" > "$ROOT/opt/gost-mini/deploy-key.pub"
fi

log "first-boot setup script"
cat > "$ROOT/opt/gost-mini/firstboot.sh" <<'FB'
#!/bin/bash
# Runs ONCE on the device: installs deps, fetches the e-paper driver, and wires
# GOST MINI up for whichever account Raspberry Pi Imager created.
set -u
exec >>/var/log/gost-mini-firstboot.log 2>&1
echo "=== $(date) gost-mini firstboot ==="

U="$(getent passwd 1000 | cut -d: -f1)"; [ -n "$U" ] || U=pi
H="$(getent passwd "$U" | cut -d: -f6)"; [ -n "$H" ] || H="/home/$U"
echo "operator: $U ($H)"

export DEBIAN_FRONTEND=noninteractive
for i in 1 2 3; do
  apt-get update -qq && break
  echo "apt update failed (try $i) -- waiting for network"; sleep 20
done
apt-get install -y --no-install-recommends \
  python3-pil python3-serial python3-numpy python3-gpiozero \
  python3-spidev python3-lgpio python3-smbus2 \
  git bluez bluez-tools rfkill avahi-daemon i2c-tools fonts-dejavu-core \
  || echo "WARNING: some packages failed"

# Waveshare driver: sparse-clone ONLY the driver folder. The full e-Paper repo
# is >1GB of STM32/Arduino demos and overruns /tmp on a Zero.
if [ ! -d /opt/gost-mini/waveshare_epd ]; then
  WS=/root/.ws-src; rm -rf "$WS"
  if git clone --depth 1 --filter=blob:none --sparse \
       https://github.com/waveshareteam/e-Paper "$WS" >/dev/null 2>&1 \
     && git -C "$WS" sparse-checkout set RaspberryPi_JetsonNano/python/lib/waveshare_epd >/dev/null 2>&1; then
    cp -r "$WS/RaspberryPi_JetsonNano/python/lib/waveshare_epd" /opt/gost-mini/waveshare_epd
    echo "waveshare driver installed"
  else
    echo "WARNING: waveshare driver fetch failed -- rerun firstboot.sh when online"
  fi
  rm -rf "$WS"
fi

# app in the user's home, matching the documented layout
if [ ! -d "$H/gost-mini" ]; then
  cp -r /opt/gost-mini "$H/gost-mini"
  rm -f "$H/gost-mini/firstboot.sh"
  chown -R "$U":"$U" "$H/gost-mini"
fi

# serial access for the OBD adapter (/dev/rfcomm0 is root:dialout)
usermod -aG dialout,bluetooth "$U" 2>/dev/null || usermod -aG dialout "$U" 2>/dev/null || true

# deploy key for the operator account
if [ -f /opt/gost-mini/deploy-key.pub ]; then
  install -d -m700 -o "$U" -g "$U" "$H/.ssh"
  touch "$H/.ssh/authorized_keys"
  grep -qxFf /opt/gost-mini/deploy-key.pub "$H/.ssh/authorized_keys" 2>/dev/null || \
    cat /opt/gost-mini/deploy-key.pub >> "$H/.ssh/authorized_keys"
  chown "$U":"$U" "$H/.ssh/authorized_keys"; chmod 600 "$H/.ssh/authorized_keys"
fi

# let the app re-establish the rfcomm link unattended
cat > /etc/sudoers.d/gost-mini <<SUD
$U ALL=(root) NOPASSWD: /usr/bin/rfcomm bind *, /usr/bin/rfcomm release *, /usr/bin/systemctl restart gost-obd-link.service
SUD
chmod 440 /etc/sudoers.d/gost-mini

for f in /etc/systemd/system/gost-mini.service \
         /etc/systemd/system/gost-mini-wifi.service \
         /etc/systemd/system/gost-obd-link.service; do
  [ -f "$f" ] && sed -i "s|__USER__|$U|g; s|__HOME__|$H|g" "$f"
done
systemctl daemon-reload
systemctl enable --now gost-mini.service gost-mini-wifi.service 2>/dev/null || true
touch /opt/gost-mini/.finalised
echo "=== firstboot complete -- panel should come up shortly ==="
FB
chmod +x "$ROOT/opt/gost-mini/firstboot.sh"

log "systemd units"
cat > "$ROOT/etc/systemd/system/gost-mini-firstboot.service" <<'UNIT'
[Unit]
Description=GOST MINI first-boot setup (runs once)
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/opt/gost-mini/.finalised

[Service]
Type=oneshot
ExecStart=/opt/gost-mini/firstboot.sh
RemainAfterExit=yes
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
UNIT

cat > "$ROOT/etc/systemd/system/gost-mini.service" <<'UNIT'
[Unit]
Description=GOST MINI (e-paper OBD code reader)
After=bluetooth.service network.target
Wants=bluetooth.service

[Service]
Type=simple
User=__USER__
SupplementaryGroups=dialout
WorkingDirectory=__HOME__/gost-mini
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u __HOME__/gost-mini/gostmini.py
Restart=always
RestartSec=8

[Install]
WantedBy=multi-user.target
UNIT

cat > "$ROOT/etc/systemd/system/gost-mini-wifi.service" <<'UNIT'
[Unit]
Description=GOST MINI Wi-Fi watchdog (power-save off + auto-reconnect + mDNS)
After=NetworkManager.service network.target
Wants=NetworkManager.service

[Service]
Type=simple
ExecStart=__HOME__/gost-mini/gost-mini-wifi.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

cat > "$ROOT/etc/systemd/system/gost-obd-link.service" <<'UNIT'
[Unit]
Description=GOST MINI supervised Bluetooth OBD link (rfcomm connect)
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
ExecStart=__HOME__/gost-mini/gost-obd-link.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# enable firstboot without systemctl (no ARM execution): just add the symlink
mkdir -p "$ROOT/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/gost-mini-firstboot.service \
       "$ROOT/etc/systemd/system/multi-user.target.wants/gost-mini-firstboot.service"

sync
cleanup
trap - EXIT

log "compressing"
mkdir -p "$OUT"
rm -f "$OUT/gost-mini.img.xz"
xz -T0 -2 -c "$IMG" > "$OUT/gost-mini.img.xz"
md5sum "$OUT/gost-mini.img.xz" | awk '{print "MD5", $1}'
ls -lh "$OUT/gost-mini.img.xz"
log "BUILD COMPLETE"
