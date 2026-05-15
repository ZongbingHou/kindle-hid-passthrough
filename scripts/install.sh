#!/bin/sh

installMainFiles()
{
  echo " -> Installing main program files"
  mkdir -p /mnt/us/kindle_hid_passthrough/dist
  mkdir -p /mnt/us/kindle_hid_passthrough/illusion/BTManager
  mkdir -p /mnt/us/kindle_hid_passthrough/cache
  cp -r dist/* /mnt/us/kindle_hid_passthrough/dist/
  cp kindle-hid-passthrough /mnt/us/kindle_hid_passthrough/
  cp libsyscall_wrapper.so /mnt/us/kindle_hid_passthrough/
  cp config.ini /mnt/us/kindle_hid_passthrough/
  chmod +x /mnt/us/kindle_hid_passthrough/kindle-hid-passthrough
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
  if [ -d /mnt/us/koreader/plugins/ ]; then
    installKOReaderPlugin
  fi
  echo ""
  echo "Installation complete. Open 'BT Manager' from the Kindle library."
}

installUdevRules()
{
  echo " -> Installing udev rules"
  chmod +x scripts/dev_is_keyboard.sh
  /usr/sbin/mntroot rw
  cp assets/99-hid-keyboard.rules /etc/udev/rules.d
  /usr/sbin/udevadm control --reload-rules
  /usr/sbin/mntroot ro
  echo " -> Ready."
}

installUpstart()
{
  echo " -> Installing upstart service"
  /usr/sbin/mntroot rw
  cp assets/hid-passthrough.upstart /etc/upstart/hid-passthrough.conf
  /usr/sbin/mntroot ro
  echo " -> Ready."
}

pairDevice()
{
  ./kindle-hid-passthrough --pair 2>&1 | grep -v "libenvload.so"
}

listDevices()
{
  cat devices.conf
}

setLayout()
{
  printf "Enter layout code (e.g. fr, de, 'fr(oss)'): "
  read layout
  /bin/sh setlayout.sh "$layout"
}

installWAFApp()
{
  echo " -> Installing BTManager app"
  cp -r illusion/BTManager/* /mnt/us/kindle_hid_passthrough/illusion/BTManager/
  cp illusion/BTManager.sh /mnt/us/kindle_hid_passthrough/illusion/BTManager.sh
  cp illusion/install-waf-app.sh /mnt/us/kindle_hid_passthrough/illusion/install-waf-app.sh
  if [ -f /mnt/us/kindle_hid_passthrough/illusion/install-waf-app.sh ]; then
    /bin/sh /mnt/us/kindle_hid_passthrough/illusion/install-waf-app.sh
  else
    echo "ERROR: /mnt/us/kindle_hid_passthrough/illusion/install-waf-app.sh not found"
  fi
}

installKOReaderPlugin()
{
  if [ ! -d /mnt/us/koreader/plugins/ ]; then
    echo " -> KOReader not found, skipping plugin install"
    return
  fi
  echo " -> Installing KOReader plugin"
  cp -r koreader-plugin/hidpassthrough.koplugin /mnt/us/koreader/plugins/hidpassthrough.koplugin
  echo " -> Ready."
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
  printf " 7) Set custom keyboard layout\n"
  printf " 8) Install KOReader plugin\n"
  printf " 9) Uninstall everything\n"
  printf "10) Quit\n"
}

while :; do
  print_menu
  printf "Enter choice [1-10]: "
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
      setLayout
      ;;
    8)
      installKOReaderPlugin
      ;;
    9)
      uninstallAll
      ;;
    10)
      echo "Exiting."
      break
      ;;
    *)
      printf "Invalid option: %s\n" "$choice"
      ;;
  esac
done
