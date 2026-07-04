#!/bin/bash
# Build uhid.ko for Kindle Oasis 1 (duet), kernel 3.0.35-lab126.
#
# Prereqs (set these to match your host):
#   SRC       - a 3.0.35-lab126 kernel tree with arch/arm/configs/imx60_duet_defconfig
#               (e.g. git clone https://github.com/katadelos/linux-3.0.35-lab126)
#   TCBIN     - dir containing arm-linux-gnueabi-gcc 4.9.x
#               (kernel.org crosstool: files/bin/x86_64/4.9.4/x86_64-gcc-4.9.4-nolibc-arm-linux-gnueabi.tar.xz)
#   COMPATLIB - dir with libmpfr.so.4 (Debian ships .so.6; grab libmpfr4_3.1.5-1
#               from archive.debian.org and extract libmpfr.so.4 here). Omit if
#               your host still has libmpfr.so.4.
#   HOSTCC    - a pre-14 gcc (gcc 14 makes implicit-function-declaration fatal,
#               which breaks 3.0.35's host tools). gcc-12 works.
set -e

: "${SRC:?set SRC to the 3.0.35-lab126 kernel tree}"
: "${TCBIN:?set TCBIN to the arm-linux-gnueabi- gcc 4.9 bin dir}"
: "${HOSTCC:=gcc-12}"

HERE=$(cd "$(dirname "$0")" && pwd)

cp "$HERE/uhid.h" "$SRC/include/linux/uhid.h"
cp "$HERE/uhid.c" "$SRC/drivers/hid/uhid.c"

# Wire uhid into the HID Makefile/Kconfig if not already present.
grep -q CONFIG_UHID "$SRC/drivers/hid/Makefile" || \
  sed -i '/^obj-\$(CONFIG_HID)\s*+= hid.o/a obj-$(CONFIG_UHID)\t\t+= uhid.o' "$SRC/drivers/hid/Makefile"
grep -q 'config UHID' "$SRC/drivers/hid/Kconfig" || \
  sed -i 's|^endmenu|config UHID\n\ttristate "User-space I/O driver support for HID subsystem"\n\tdepends on HID\n\tdefault n\n\n&|' "$SRC/drivers/hid/Kconfig"

cd "$SRC"
export ARCH=arm CROSS_COMPILE=arm-linux-gnueabi-
export PATH="$TCBIN:$PATH"
[ -n "$COMPATLIB" ] && export LD_LIBRARY_PATH="$COMPATLIB:$LD_LIBRARY_PATH"
HOSTOPTS="HOSTCC=$HOSTCC HOSTCFLAGS=-fcommon"

make $HOSTOPTS imx60_duet_defconfig
# duet defconfig gates off the whole HID subsystem, and the stock kernel has no
# HID core at all, so build both hid.ko (hid-core + hid-input) and uhid.ko as
# modules. Disable BT_HIDP/USB_HID so nothing 'select's HID=y (we need =m to get
# a loadable hid.ko).
scripts/config --file .config --enable HID_SUPPORT --module HID --module UHID \
    --disable BT_HIDP --disable USB_HID
yes '' | make $HOSTOPTS oldconfig >/dev/null 2>&1 || true
make $HOSTOPTS modules_prepare
make $HOSTOPTS M=drivers/hid modules

for m in hid uhid; do
  echo "built: $SRC/drivers/hid/$m.ko"
  readelf -A "drivers/hid/$m.ko" | grep Tag_CPU_arch
  "$TCBIN/arm-linux-gnueabi-strings" "drivers/hid/$m.ko" | grep vermagic
done
