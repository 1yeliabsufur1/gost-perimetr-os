#!/usr/bin/env bash
# GOST/PERIMETR OS installer. Runs as root, either interactively (manual
# re-run/update) or once from gost-firstboot.service on first boot.
# Idempotent: safe to run again (e.g. to pick up an updated checkout).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh: must run as root (sudo $0)" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOST_HOME=/opt/gost

log() { echo "[gost-install] $*"; }

# ---------------------------------------------------------------------------
# 0. Wait for cloud-init to finish BEFORE looking at users. Pi OS trixie
#    boots with a temporary 'pi' user and cloud-init RENAMES it to the
#    userconf.txt name mid-boot. Detecting the user before the rename
#    finished made a later `chown pi:pi` explode with "invalid user" on
#    real hardware (2026-07-04) and killed the whole install.
# ---------------------------------------------------------------------------
if command -v cloud-init >/dev/null 2>&1; then
  log "waiting for cloud-init to finish user provisioning..."
  timeout 180 cloud-init status --wait >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 1. Resolve the operator user. The firstboot service runs with NO
#    SUDO_USER -- never assume it's set. Prefer the image's own 'gost'
#    account (created via boot/userconf.txt), then the uid-1000 account,
#    then create 'gost' as a last resort.
# ---------------------------------------------------------------------------
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  GOST_USER="${SUDO_USER}"
elif id -u gost >/dev/null 2>&1; then
  GOST_USER="gost"
elif getent passwd 1000 >/dev/null 2>&1; then
  GOST_USER="$(getent passwd 1000 | cut -d: -f1)"
else
  GOST_USER="gost"
  useradd -m -s /bin/bash "$GOST_USER"
fi
log "operator user: $GOST_USER"
echo "$GOST_USER" > /etc/gost-user

# Wi-Fi ships rfkill-soft-blocked on Pi OS until a country is set; unblock
# here so the wizard's Wi-Fi join actually works.
rfkill unblock wifi 2>/dev/null || true
rfkill unblock bluetooth 2>/dev/null || true   # BT pairing UI + wireless OBD

# Wi-Fi regulatory domain. WITHOUT a country the kernel regdom stays "00"
# (world), most channels are disabled, and the radio can't scan/join -- the
# single most common "wifi doesn't work" cause on a Pi. Set it persistently.
# Override the default by exporting GOST_WIFI_COUNTRY, or edit
# /etc/gost-wifi-country on the device (gost-net.service reads it every boot).
WIFI_COUNTRY="${GOST_WIFI_COUNTRY:-US}"
echo "$WIFI_COUNTRY" > /etc/gost-wifi-country
raspi-config nonint do_wifi_country "$WIFI_COUNTRY" 2>/dev/null || true
iw reg set "$WIFI_COUNTRY" 2>/dev/null || true

# Pi OS ships the default account with shell /usr/sbin/nologin (a placeholder
# meant to be "renamed" on first boot). We reuse it as the operator, so give it
# a real login shell or SSH is refused even with a valid key (bailey 2026-07-25:
# "Permission denied" over SSH though the kiosk ran fine).
CUR_SHELL="$(getent passwd "$GOST_USER" | cut -d: -f7)"
case "$CUR_SHELL" in
  */nologin|*/false|"") usermod -s /bin/bash "$GOST_USER" && log "gave $GOST_USER a login shell (was ${CUR_SHELL:-none})" ;;
esac

# Deploy key: if the image ships a public key, authorize it for the operator so
# a maintainer can SSH in without a password. Not committed to the repo -- an
# image builder drops deploy-key.pub beside install.sh (or a *.pub in the boot
# partition) to enable it. Harmless/no-op when no key is baked.
for KEYSRC in "$SRC_DIR/deploy-key.pub" /opt/gost/deploy-key.pub \
              /boot/firmware/gost-authorized-key.pub /boot/gost-authorized-key.pub; do
  [ -f "$KEYSRC" ] || continue
  UHOME="$(getent passwd "$GOST_USER" | cut -d: -f6)"
  [ -n "$UHOME" ] || continue
  install -d -m700 -o "$GOST_USER" -g "$GOST_USER" "$UHOME/.ssh"
  touch "$UHOME/.ssh/authorized_keys"
  grep -qxFf "$KEYSRC" "$UHOME/.ssh/authorized_keys" 2>/dev/null || cat "$KEYSRC" >> "$UHOME/.ssh/authorized_keys"
  chown "$GOST_USER":"$GOST_USER" "$UHOME/.ssh/authorized_keys"
  chmod 600 "$UHOME/.ssh/authorized_keys"
  log "authorized deploy key ($KEYSRC) for $GOST_USER"
  break
done
# Belt-and-suspenders: the image ships /boot/ssh, but make sure sshd is enabled.
systemctl enable ssh 2>/dev/null || systemctl enable sshd 2>/dev/null || true

# Chromium enterprise policy: the kiosk browser must never offer to save
# passwords (the Wi-Fi PSK field triggered Chrome's "save password?" bubble
# over the NAV tab -- bailey 2026-07-13). Policy beats flags: flags like
# --disable-save-password-bubble come and go between Chromium versions.
for CHROMIUM_ETC in /etc/chromium /etc/chromium-browser; do
  mkdir -p "$CHROMIUM_ETC/policies/managed"
  cat > "$CHROMIUM_ETC/policies/managed/gost-kiosk.json" <<'POLICY'
{
  "PasswordManagerEnabled": false,
  "AutofillCreditCardEnabled": false,
  "AutofillAddressEnabled": false,
  "TranslateEnabled": false,
  "DefaultNotificationsSetting": 2
}
POLICY
done

# Identity: this is a GOST head unit, not a stock "raspberrypi".
if [ "$(hostname)" != "gost" ]; then
  hostnamectl set-hostname gost 2>/dev/null || echo gost > /etc/hostname
  sed -i 's/\braspberrypi\b/gost/g' /etc/hosts 2>/dev/null || true
fi
nmcli radio wifi on 2>/dev/null || true

for grp in dialout gpio spi i2c video render seat netdev sudo; do
  getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" "$GOST_USER" || true
done

# ---------------------------------------------------------------------------
# 2 + 3. Dependencies (apt packages + Python venv).
#
#    THE BIG WIN: if the image was built "fat" (tools/provision.sh already
#    ran inside it at build time via qemu ARM chroot), /opt/gost/.provisioned
#    exists and this ENTIRE block is skipped -- first boot needs NO internet,
#    NO clock sync, and finishes in seconds. This is what finally kills the
#    recurring first-boot install failures (network/clock/apt-signature).
#
#    On a stock (thin) image or a manual re-run, the block runs normally.
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive

if [ -f "$GOST_HOME/.provisioned" ]; then
  log "dependencies already baked into image -- skipping apt/pip (offline first boot)"
  systemctl enable --now seatd 2>/dev/null || true
else
  # network-online.target can fire before DHCP actually lands on some trixie
  # setups. Wait up to 5 minutes for real connectivity.
  log "waiting for network..."
  for _ in $(seq 1 60); do
    if curl -fs --max-time 5 -o /dev/null http://deb.debian.org 2>/dev/null || \
       ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then
      log "network is up"; break
    fi
    sleep 5
  done

  # The Pi has no RTC: on first boot the clock can be behind, and apt's sqv
  # signature check then fails with "signature not live until <future>".
  # Wait for NTP sync (up to 3 min) before touching apt.
  log "waiting for clock sync (NTP)..."
  for _ in $(seq 1 36); do
    if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
      log "clock is synced: $(date)"; break
    fi
    sleep 5
  done

  bash "$SRC_DIR/tools/provision.sh"
fi

# ---------------------------------------------------------------------------
# 4. Copy project tree into place. Never clobber media/ or state/ that a
#    prior install already populated with the operator's own content.
# ---------------------------------------------------------------------------
mkdir -p "$GOST_HOME"
rsync -a --exclude='media/' --exclude='state/' --exclude='.git/' "$SRC_DIR"/ "$GOST_HOME"/

if [ ! -d "$GOST_HOME/media" ]; then
  mkdir -p "$GOST_HOME/media/TV/COMMERCIALS" "$GOST_HOME/media/MUSIC" "$GOST_HOME/media/PODCASTS"
  [ -d "$SRC_DIR/media" ] && cp -r "$SRC_DIR"/media/. "$GOST_HOME/media/" 2>/dev/null || true
fi
# Every channel 03-82 gets its own folder so content never has to share:
# drop files in, rename the folder ("07 CARTOONS") to name the channel.
for n in $(seq -w 3 82); do
  ls -d "$GOST_HOME/media/TV/$n "* >/dev/null 2>&1 || mkdir -p "$GOST_HOME/media/TV/$n CH"
done
# Example seasonal folders so the holiday auto-switching is discoverable: any
# channel (and COMMERCIALS) can hold Holiday subfolders that take over on the
# date. Ship the folders empty as a template; users drop videos in.
for h in Halloween Thanksgiving Christmas NewYears Valentines July4th; do
  mkdir -p "$GOST_HOME/media/TV/COMMERCIALS/$h"
done
mkdir -p "$GOST_HOME/media/TV/07 CH/Halloween" "$GOST_HOME/media/TV/12 CH/Christmas"
mkdir -p "$GOST_HOME/media/MUSIC"/{ROCK,POP,RAP,COUNTRY,JAZZ,METAL,ELECTRONIC,CLASSICAL} \
         "$GOST_HOME/media/PODCASTS"/{NEWS,COMEDY,"TRUE CRIME",TECH,SPORTS,HISTORY}
mkdir -p "$GOST_HOME/maps" "$GOST_HOME/state"

chown -R "$GOST_USER":"$GOST_USER" "$GOST_HOME"
chmod +x "$GOST_HOME/install.sh" "$GOST_HOME/system/kiosk-start.sh" \
  "$GOST_HOME/system/gost-setpass" "$GOST_HOME/system/gost-settime" \
  "$GOST_HOME/system/gost-power" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Privileged helpers (password/clock never touch argv -- piped via stdin)
#    + sudoers whitelist restricted to these exact paths.
# ---------------------------------------------------------------------------
install -m 750 -o root -g root "$GOST_HOME/system/gost-setpass" /usr/local/sbin/gost-setpass
install -m 750 -o root -g root "$GOST_HOME/system/gost-settime" /usr/local/sbin/gost-settime
install -m 750 -o root -g root "$GOST_HOME/system/gost-power" /usr/local/sbin/gost-power
# Ford-PID capture tool (doors/TPMS/oil-life mapping): logs to the FAT boot
# partition so the operator can pull them off the SD card from a PC.
install -m 0755 "$GOST_HOME/tools/obd_probe.py" /usr/local/bin/gost-obd-probe

SUDOERS_TMP="$(mktemp)"
sed "s/__GOST_USER__/$GOST_USER/g" "$GOST_HOME/system/sudoers-gost" > "$SUDOERS_TMP"
visudo -c -f "$SUDOERS_TMP"
install -m 440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/gost
rm -f "$SUDOERS_TMP"

# NetworkManager polkit: without this, nmcli from the kiosk backend fails
# with "Not authorized to control networking" (seen on hardware 2026-07-12)
# even with the user in netdev -- polkit checks the *session*, and a systemd
# service has no seat/session. Grant the operator user NM control directly.
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-gost-networkmanager.rules <<POLKIT
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "$GOST_USER") {
        return polkit.Result.YES;
    }
});
POLKIT
systemctl try-restart polkit 2>/dev/null || true

# ---------------------------------------------------------------------------
# 6. systemd units (User= is templated per-install since it depends on the
#    detected operator account).
# ---------------------------------------------------------------------------
for unit in hud-backend.service hud-kiosk.service; do
  sed "s/__GOST_USER__/$GOST_USER/g" "$GOST_HOME/system/$unit" > "/etc/systemd/system/$unit"
done

# OBD-over-Bluetooth (Part 1): OBDLink MX+ SPP link. Non-destructive -- ships
# its own unit + a drop-in that only adds ordering to the backend.
if command -v rfcomm >/dev/null 2>&1; then
  install -m0644 "$GOST_HOME/system/bluetooth-main.conf" /etc/bluetooth/main.conf
  install -m0755 "$GOST_HOME/system/obd-bt-pair.sh"      /usr/local/bin/obd-bt-pair.sh
  install -m0755 "$GOST_HOME/system/obd-rfcomm-link.sh"  /usr/local/bin/obd-rfcomm-link.sh
  install -m0644 "$GOST_HOME/system/obd-rfcomm.service"  /etc/systemd/system/obd-rfcomm.service
  install -d /etc/systemd/system/hud-backend.service.d
  install -m0644 "$GOST_HOME/system/hud-backend.service.d/10-rfcomm.conf" \
    /etc/systemd/system/hud-backend.service.d/10-rfcomm.conf
  command -v sdptool >/dev/null 2>&1 || \
    log "WARNING: sdptool absent -- SPP channel auto-discovery off; pinning channel 1"
else
  log "WARNING: rfcomm not present (bluez) -- OBD Bluetooth transport unavailable; USB OBD still works"
fi

systemctl daemon-reload
# Disable first so a stale wants-symlink from an earlier install (e.g. the
# old graphical.target.wants/hud-kiosk.service) is removed before we re-point
# it at multi-user.target. Without this, re-running the installer would leave
# the kiosk wanted only by the never-reached graphical.target.
systemctl disable hud-backend.service hud-kiosk.service 2>/dev/null || true
systemctl enable obd-rfcomm.service 2>/dev/null || true

# Force the Wi-Fi radio on at every boot (bailey: it boots soft-disabled).
install -m0644 "$GOST_HOME/system/gost-net.service" /etc/systemd/system/gost-net.service
systemctl enable gost-net.service 2>/dev/null || true
# Kill Wi-Fi power-save permanently -- the radio sleeping when idle drops the
# connection mid-session (bailey 2026-07-14). This makes it stick across
# reconnects/reboots at the NetworkManager level.
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/gost-wifi-powersave.conf <<'NMPS'
[connection]
wifi.powersave = 2
NMPS
systemctl enable hud-backend.service
systemctl enable hud-kiosk.service
# Pi OS Lite already defaults to multi-user.target, but pin it so nothing
# (a stray desktop install, a customization) can switch us to a graphical
# target the kiosk isn't wired for.
systemctl set-default multi-user.target 2>/dev/null || true
# Mask getty on tty1 entirely: the kiosk owns that VT, and Conflicts= alone
# still let a login prompt flash on some boots (bailey: "terminal on initial
# load/bootup"). Masking guarantees no getty ever starts there. Recovery is
# always SSH or another VT (Ctrl-Alt-F2), so this doesn't lock anyone out.
systemctl mask getty@tty1.service 2>/dev/null || true

# Quiet the boot console so text/log spam doesn't show before the dashboard.
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
if [ -f "$CMDLINE" ] && ! grep -q 'gost-quiet' "$CMDLINE"; then
  sed -i 's/$/ quiet loglevel=3 vt.global_cursor_default=0 logo.nologo consoleblank=0 gost-quiet/' "$CMDLINE"
fi

# ---------------------------------------------------------------------------
# 7. SPI/I2C dtparam for MCP3008 pots / optional hardware, if not already set.
#    (The image-build workflow also does this; kept here too so a stock,
#    non-custom-image install still ends up with working SPI.)
# ---------------------------------------------------------------------------
CONFIG_TXT=/boot/firmware/config.txt
[ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
if [ -f "$CONFIG_TXT" ]; then
  grep -q '^dtparam=spi=on' "$CONFIG_TXT" || echo 'dtparam=spi=on' >> "$CONFIG_TXT"
  grep -q '^dtparam=i2c_arm=on' "$CONFIG_TXT" || echo 'dtparam=i2c_arm=on' >> "$CONFIG_TXT"
fi

# ---------------------------------------------------------------------------
# 8. Done. Mark installed and reboot straight into the kiosk.
#
#    Two hard-won rules from real-hardware failure (2026-07-04):
#    - NEVER `systemctl disable gost-firstboot` from inside this script:
#      disabling the very unit whose start job is still running wedged
#      systemctl on trixie and the whole install died on a start timeout.
#      The unit's ConditionPathExists=!/opt/gost/.installed already
#      guarantees it never runs again -- the marker file is the off switch.
#    - NEVER call `reboot` synchronously from inside the unit either: the
#      shutdown transaction conflicts with our own still-running start job.
#      Schedule it detached so this script exits 0 and completes its start
#      job cleanly, THEN the reboot fires.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 8b. FINAL VALIDATION -- surface a broken install BEFORE the reboot instead
#     of booting into a black screen. If anything critical is missing we do
#     NOT mark .installed, so gost-firstboot retries on the next boot.
#     (The py_compile check here would have caught a hud_server.py that got
#     accidentally overwritten with non-Python content.)
# ---------------------------------------------------------------------------
log "running final validation..."
ok=1
vfail() { log "VALIDATION FAILED: $*"; ok=0; }
for f in "$GOST_HOME/backend/hud_server.py" "$GOST_HOME/system/kiosk-start.sh" \
         "/etc/systemd/system/hud-backend.service" "/etc/systemd/system/hud-kiosk.service"; do
  [ -f "$f" ] || vfail "missing $f"
done
[ -x "$GOST_HOME/venv/bin/python3" ] || vfail "python venv missing"
command -v cage >/dev/null || vfail "cage missing"
command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 || vfail "chromium missing"
"$GOST_HOME/venv/bin/python3" -m py_compile "$GOST_HOME/backend/hud_server.py" 2>/dev/null \
  || vfail "hud_server.py does not compile as Python"
systemctl cat hud-backend.service >/dev/null 2>&1 || vfail "hud-backend.service invalid"
systemctl cat hud-kiosk.service   >/dev/null 2>&1 || vfail "hud-kiosk.service invalid"

if [ "$ok" -ne 1 ]; then
  log "install INCOMPLETE -- not marking done; firstboot will retry next boot."
  log "For a console now: Ctrl+Alt+F2."
  exit 1
fi
log "validation OK: backend compiles, venv/chromium/cage/units all present"

touch "$GOST_HOME/.installed"

log "install complete -- rebooting into kiosk in a few seconds"
sync
if [ -d /run/systemd/system ] && command -v systemd-run >/dev/null 2>&1; then
  systemd-run --on-active=3 --quiet systemctl reboot || { (sleep 3; reboot) & }
else
  (sleep 3; reboot) &
fi
exit 0
