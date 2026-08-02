#!/bin/sh

INSTALL_DIR="/mnt/us/kindle_hid_passthrough"

SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

# True when source == destination, so the cp commands below would be a no-op.
in_install_dir()
{
  [ "$SRC_DIR" = "$INSTALL_DIR" ]
}

koreaderPluginDir()
{
  for base in /mnt/us/koreader /mnt/base-us/koreader; do
    if [ -d "$base/plugins" ]; then
      echo "$base/plugins"
      return 0
    fi
  done
  return 1
}

installMainFiles()
{
  echo " -> Installing main program files"
  mkdir -p "$INSTALL_DIR/dist" "$INSTALL_DIR/illusion/BTManager" "$INSTALL_DIR/cache"
  if ! in_install_dir; then
    cp -r "$SRC_DIR/dist/"* "$INSTALL_DIR/dist/"
    cp "$SRC_DIR/kindle-hid-passthrough" "$INSTALL_DIR/"
    cp "$SRC_DIR/libsyscall_wrapper.so" "$INSTALL_DIR/"
    cp "$SRC_DIR/config.ini" "$INSTALL_DIR/"
    cp -r "$SRC_DIR/scripts" "$SRC_DIR/assets" "$INSTALL_DIR/"
  fi
  chmod +x "$INSTALL_DIR/kindle-hid-passthrough"
  echo " -> Ready."
}

installAll()
{
  echo ""
  echo "=== Full Install ==="
  installUdevRules
  installUpstart
  installMainFiles
  installWAFApp
  if ! installKOReaderPlugin; then
    echo ""
    echo "Install finished WITH ERRORS: the KOReader plugin was not installed."
    return 1
  fi
  echo ""
  echo "Installation complete. Open 'BT Manager' from the Kindle library."
}

installUdevRules()
{
  echo " -> Installing udev rules"
  /usr/sbin/mntroot rw
  cp "$SRC_DIR/assets/99-hid-keyboard.rules" /etc/udev/rules.d
  /usr/sbin/udevadm control --reload-rules
  /usr/sbin/mntroot ro
  echo " -> Ready."
}

installUpstart()
{
  echo " -> Installing upstart service"
  /usr/sbin/mntroot rw
  cp "$SRC_DIR/assets/hid-passthrough.upstart" /etc/upstart/hid-passthrough.conf
  /usr/sbin/mntroot ro
  echo " -> Ready."
}

pairDevice()
{
  (cd "$INSTALL_DIR" && ./kindle-hid-passthrough --pair 2>&1 | grep -v "libenvload.so")
}

listDevices()
{
  cat "$INSTALL_DIR/devices.conf"
}

installWAFApp()
{
  echo " -> Installing BTManager app"
  if ! in_install_dir; then
    mkdir -p "$INSTALL_DIR/illusion/BTManager"
    cp -r "$SRC_DIR/illusion/BTManager/"* "$INSTALL_DIR/illusion/BTManager/"
    cp "$SRC_DIR/illusion/BTManager.sh" "$INSTALL_DIR/illusion/BTManager.sh"
    cp "$SRC_DIR/illusion/install-waf-app.sh" "$INSTALL_DIR/illusion/install-waf-app.sh"
  fi
  if [ -f "$INSTALL_DIR/illusion/install-waf-app.sh" ]; then
    /bin/sh "$INSTALL_DIR/illusion/install-waf-app.sh"
  else
    echo "ERROR: $INSTALL_DIR/illusion/install-waf-app.sh not found"
  fi
}

installKOReaderPlugin()
{
  PLUGINS_DIR=$(koreaderPluginDir) || {
    echo " -> KOReader not found, skipping plugin install"
    return 0
  }
  SRC_PLUGIN="$SRC_DIR/koreader-plugin/hidpassthrough.koplugin"
  if [ ! -f "$SRC_PLUGIN/main.lua" ]; then
    echo "ERROR: plugin source not found at $SRC_PLUGIN" >&2
    echo "       Run this script from the extracted release, not a partial copy." >&2
    return 1
  fi

  echo " -> Installing KOReader plugin into $PLUGINS_DIR"
  DEST="$PLUGINS_DIR/hidpassthrough.koplugin"
  rm -rf "$DEST"
  if ! cp -r "$SRC_PLUGIN" "$DEST"; then
    echo "ERROR: failed to copy the plugin to $DEST" >&2
    return 1
  fi

  for f in main.lua _meta.lua event_map_extra.lua; do
    if [ ! -f "$DEST/$f" ]; then
      echo "ERROR: $f is missing from $DEST" >&2
      return 1
    fi
  done
  echo " -> Ready. Restart KOReader to load it."
}

uninstallAll()
{
  echo ""
  echo "=== Uninstall ==="
  printf "This will stop the daemon, remove udev/upstart/WAF app, and delete the install directory.\n"
  printf "Continue? [y/N]: "
  read confirm
  case "$confirm" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; return ;;
  esac

  APP_ID="com.lzampier.btmanager"
  INSTALL_DIR="/mnt/us/kindle_hid_passthrough"
  SCRIPTLET_DEST="/mnt/us/documents/BTManager.sh"
  APPREG_DB="/var/local/appreg.db"

  echo " -> Stopping daemon"
  /sbin/stop hid-passthrough 2>/dev/null
  pkill -f "kindle-hid-passthrough" 2>/dev/null
  pkill -f "main.py --daemon" 2>/dev/null
  pkill -f "ld-linux-armhf." 2>/dev/null

  /usr/sbin/mntroot rw

  echo " -> Removing upstart config"
  rm -f /etc/upstart/hid-passthrough.conf

  echo " -> Removing udev rules"
  rm -f /etc/udev/rules.d/99-hid-keyboard.rules
  if [ -f /usr/local/bin/dev_is_keyboard.sh ]; then
    rm -f /usr/local/bin/dev_is_keyboard.sh
  fi
  /usr/sbin/udevadm control --reload-rules 2>/dev/null

  echo " -> Unregistering WAF app"
  if [ -f "$APPREG_DB" ]; then
    sqlite3 "$APPREG_DB" <<EOF 2>/dev/null
DELETE FROM properties WHERE handlerId='$APP_ID';
DELETE FROM associations WHERE handlerId='$APP_ID';
DELETE FROM handlerIds WHERE handlerId='$APP_ID';
EOF
  fi
  rm -f "$SCRIPTLET_DEST"

  /usr/sbin/mntroot ro

  echo " -> Removing install directory $INSTALL_DIR"
  cd /tmp
  rm -rf "$INSTALL_DIR"

  echo ""
  echo "Uninstall complete. Reboot recommended."
}

print_menu()
{
  printf "\nSelect an option:\n"
  printf " 1) Install everything (recommended)\n"
  printf " 2) Pair Bluetooth keyboard\n"
  printf " 3) List paired devices\n"
  printf " 4) Install udev rules (keyboard service)\n"
  printf " 5) Install upstart (auto-start on boot)\n"
  printf " 6) Install BTManager app\n"
  printf " 7) Install KOReader plugin\n"
  printf " 8) Uninstall everything\n"
  printf " 9) Quit\n"
}

# Non-interactive entry point: `sh install.sh <action>` runs one action and exits.
if [ $# -gt 0 ]; then
  case "$1" in
    installAll)         installAll; exit $? ;;
    installUdevRules)   installUdevRules; exit $? ;;
    installUpstart)     installUpstart; exit $? ;;
    installMainFiles)   installMainFiles; exit $? ;;
    installWAFApp)      installWAFApp; exit $? ;;
    installKOReaderPlugin) installKOReaderPlugin; exit $? ;;
    uninstallAll)       uninstallAll; exit $? ;;
    *) echo "Unknown action: $1" >&2; exit 1 ;;
  esac
fi

while :; do
  print_menu
  printf "Enter choice [1-9]: "
  read choice
  case "$choice" in
    1)
      installAll
      ;;
    2)
      pairDevice
      ;;
    3)
      listDevices
      ;;
    4)
      installUdevRules
      ;;
    5)
      installUpstart
      ;;
    6)
      installWAFApp
      ;;
    7)
      installKOReaderPlugin
      ;;
    8)
      uninstallAll
      ;;
    9)
      echo "Exiting."
      break
      ;;
    *)
      printf "Invalid option: %s\n" "$choice"
      ;;
  esac
done
