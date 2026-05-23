#!/bin/bash
# One-time helper: install moki systemd units after Mello→Moki rename.
# Run with: sudo bash pi/finish-rename-services.sh
# Stops old services first to free RAM on memory-limited Pis.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash pi/finish-rename-services.sh"
  exit 1
fi

CODE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$CODE_DIR"

if [ -f .moki-env ]; then
  # shellcheck disable=SC1091
  source .moki-env
fi
MOKI_USER="${MOKI_USER:-${SUDO_USER:-$USER}}"
MOKI_HOME="${MOKI_HOME:-$(getent passwd "$MOKI_USER" | cut -d: -f6)}"
MOKI_UID="${MOKI_UID:-$(id -u "$MOKI_USER")}"

echo "Stopping old services (free RAM)..."
systemctl stop mello-native mello-librespot moki-native moki-librespot 2>/dev/null || true
sleep 1

echo "Installing moki systemd services for user=$MOKI_USER home=$MOKI_HOME"
for tmpl in pi/systemd/*.service.template; do
  [ -f "$tmpl" ] || continue
  name=$(basename "$tmpl" .template)
  sed -e "s|__USER__|$MOKI_USER|g" \
      -e "s|__HOME__|$MOKI_HOME|g" \
      -e "s|__UID__|$MOKI_UID|g" \
      "$tmpl" > "/etc/systemd/system/$name"
done
for f in pi/systemd/*.service; do
  [ -f "$f" ] || continue
  ln -sf "$CODE_DIR/$f" "/etc/systemd/system/$(basename "$f")"
done

systemctl disable mello-native mello-librespot mello-touch-fix 2>/dev/null || true
rm -f /etc/systemd/system/mello-native.service \
      /etc/systemd/system/mello-librespot.service \
      /etc/systemd/system/mello-touch-fix.service

systemctl daemon-reload
systemctl enable moki-librespot moki-native moki-touch-fix
systemctl start moki-librespot moki-native

if [ ! -f /etc/sudoers.d/moki-wifi ]; then
  TMP="/tmp/moki-sudoers-finish.$$"
  echo "$MOKI_USER ALL=(ALL) NOPASSWD: /usr/local/bin/wifi-connect, /usr/bin/nmcli, /usr/sbin/iw, /bin/systemctl stop moki-librespot, /bin/systemctl start moki-librespot, /bin/systemctl restart moki-native, /bin/systemctl restart bluetooth, /usr/bin/hciconfig hci0 up, /usr/sbin/hciconfig hci0 up, /usr/bin/hciconfig hci0 down, /usr/sbin/hciconfig hci0 down, /usr/sbin/rfkill unblock bluetooth, /usr/bin/systemctl poweroff" > "$TMP"
  if visudo -cf "$TMP"; then
    install -m 440 "$TMP" /etc/sudoers.d/moki-wifi
    rm -f /etc/sudoers.d/mello-wifi
    echo "sudoers updated (moki-wifi)"
  else
    echo "WARNING: sudoers validation failed — WiFi-Setup may not work"
    rm -f "$TMP"
  fi
fi

echo "Done. Check: systemctl status moki-native"
