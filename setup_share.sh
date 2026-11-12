#!/bin/bash
# One-time setup: connect the TrueNAS (SMB) share to the Pi, read-only.
#
#   sudo ./setup_share.sh
#
# Reads the server, share name and mount point from photoframe.conf, asks for
# the share's username and password, and sets the Pi up to connect the share
# automatically (and reconnect after outages). The password is stored in
# /etc/pi-frame/share-credentials, readable only by root.
#
# Run it again any time the server address, share name or password changes.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo $0"
    exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/photoframe.conf"
CRED_DIR=/etc/pi-frame
CRED="$CRED_DIR/share-credentials"
MARKER="# pi-frame-share (added by setup_share.sh)"

# The person who ran sudo owns the files as seen by the frame
OWNER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
OWNER_UID="$(id -u "$OWNER")"
OWNER_GID="$(id -g "$OWNER")"

read_setting() {
    python3 - "$CONF" "$1" "$2" <<'EOF'
import configparser, sys
cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
cfg.read(sys.argv[1], encoding="utf-8")
print(cfg.get(sys.argv[2], sys.argv[3], fallback="").strip())
EOF
}

[ -f "$CONF" ] || { echo "Can't find $CONF"; exit 1; }
SERVER="$(read_setting share server)"
SHARE="$(read_setting share share_name)"
MOUNT="$(read_setting share mount_point)"
if [ -z "$SERVER" ] || [ -z "$SHARE" ] || [ -z "$MOUNT" ]; then
    echo "server, share_name and mount_point must all be set in the [share] section of $CONF"
    exit 1
fi

echo "Share:       //$SERVER/$SHARE"
echo "Connects at: $MOUNT (read-only)"
echo

# 1. The tool the Pi needs to connect to SMB shares
if ! command -v mount.cifs >/dev/null; then
    echo "Installing cifs-utils..."
    apt-get update -q
    apt-get install -y -q cifs-utils
fi

# 2. Username and password, stored where only root can read them
KEEP=n
if [ -f "$CRED" ]; then
    read -rp "Keep the saved username and password? [Y/n] " KEEP
    case "$KEEP" in [nN]*) KEEP=n ;; *) KEEP=y ;; esac
fi
if [ "$KEEP" != y ]; then
    read -rp "Share username: " SHARE_USER
    read -rsp "Share password: " SHARE_PASS
    echo
    mkdir -p "$CRED_DIR"
    chmod 700 "$CRED_DIR"
    umask 077
    printf 'username=%s\npassword=%s\n' "$SHARE_USER" "$SHARE_PASS" > "$CRED"
    chmod 600 "$CRED"
    unset SHARE_PASS
fi

# 3. Stop any earlier connection made by this script
UNIT="$(systemd-escape -p --suffix=automount "$MOUNT")"
systemctl stop "$UNIT" 2>/dev/null || true
umount -l "$MOUNT" 2>/dev/null || true
mkdir -p "$MOUNT"

# 4. Add the share to /etc/fstab (replacing an earlier line from this script)
[ -f /etc/fstab.pi-frame-backup ] || cp /etc/fstab /etc/fstab.pi-frame-backup
OPTIONS="credentials=$CRED,ro,uid=$OWNER_UID,gid=$OWNER_GID,file_mode=0444,dir_mode=0555"
OPTIONS="$OPTIONS,iocharset=utf8,soft,echo_interval=10,nofail,_netdev"
OPTIONS="$OPTIONS,x-systemd.automount,x-systemd.mount-timeout=20"
python3 - "$MARKER" "//$SERVER/$SHARE" "$MOUNT" "$OPTIONS" <<'EOF'
import sys
marker, source, mount, options = sys.argv[1:]
esc = lambda s: s.replace("\\", "\\134").replace(" ", "\\040")
with open("/etc/fstab", encoding="utf-8") as f:
    lines = f.read().splitlines()
kept, skip_next = [], False
for line in lines:
    if skip_next:
        skip_next = False
        continue
    if line.strip() == marker:
        skip_next = True
        continue
    kept.append(line)
kept += [marker, f"{esc(source)} {esc(mount)} cifs {options} 0 0"]
with open("/etc/fstab", "w", encoding="utf-8") as f:
    f.write("\n".join(kept) + "\n")
EOF

# 5. Turn it on and test it
systemctl daemon-reload
systemctl start "$UNIT"

echo "Testing the connection..."
if YEARS="$(timeout 40 ls "$MOUNT" 2>&1)"; then
    FOUND="$(echo "$YEARS" | grep -E '^[0-9]{4}$' | tr '\n' ' ')"
    if [ -n "$FOUND" ]; then
        echo "Connected. Year folders found: $FOUND"
    else
        echo "Connected, but no year folders (like 2012) were found at the top of the share."
        echo "Contents: $(echo "$YEARS" | head -n 10 | tr '\n' ' ')"
    fi
else
    echo "Could not connect: $YEARS"
    echo "Check the server address and share name in photoframe.conf, and the username"
    echo "and password, then run this script again. More detail: sudo dmesg | tail"
    exit 1
fi
