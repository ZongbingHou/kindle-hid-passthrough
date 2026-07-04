#!/usr/bin/env python3
"""Bluetooth hardware setup — select and drive the per-Kindle chip backend."""

import os
import re
import stat

from bt_brcm import BrcmChip
from bt_chip import run
from bt_mtk import MtkChip
from kindle_detect import detect_codename, detect_kindle
from logging_utils import log

VERSION_TXT = '/etc/version.txt'


def _module_search_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    base = os.environ.get('KINDLE_HID_BASE', '/mnt/us/kindle_hid_passthrough')
    dirs = [
        os.path.join(here, 'modules'),
        os.path.join(base, 'dist', 'kindle_hid_passthrough', 'modules'),
        os.path.join(base, 'modules'),
    ]
    seen = set()
    return [d for d in dirs if not (d in seen or seen.add(d))]


def _find_bundled_ko(name):
    for d in _module_search_dirs():
        ko = os.path.join(d, name)
        if os.path.exists(ko):
            return ko
    return None

_chip = None


def chip():
    """The BT chip backend for this Kindle (memoized; one chip per device)."""
    global _chip
    if _chip is None:
        _chip = make_chip(detect_kindle())
    return _chip


def make_chip(kindle):
    """Pick the chip backend for a detected Kindle."""
    if kindle and kindle.transport_scheme == 'serial':
        return BrcmChip(kindle)
    return MtkChip(kindle)


def _read_version_line():
    try:
        with open(VERSION_TXT) as f:
            return f.readline().strip()
    except OSError:
        return None


def _read_firmware_build():
    line = _read_version_line()
    if not line:
        return None
    m = re.search(r'-(\d+)\s*$', line)
    return m.group(1) if m else None


def _log_missing_kmod(codename, expected=None):
    kindle = detect_kindle()
    log.error("no bundled uhid.ko for this Kindle; we need to build one")
    if expected:
        log.error(f"  module needed : {expected}")
    log.error(f"  model         : {kindle.model_name if kindle else 'unknown'}")
    log.error(f"  codename      : {codename}")
    log.error(f"  kernel        : {os.uname().release}")
    log.error(f"  version.txt   : {_read_version_line() or 'unreadable'}")
    log.error(f"  searched      : {', '.join(_module_search_dirs())}")
    log.error("  ^ open an issue with these lines so we can compile the module")


def _create_dev_node(sysfs_dev, dev_path):
    """Create a char device node from a sysfs 'major:minor' dev attribute.

    Needed on kernels without devtmpfs (e.g. Oasis 1 / duet), where
    misc_register does not auto-create /dev/uhid.
    """
    try:
        with open(sysfs_dev) as f:
            major, minor = (int(x) for x in f.read().strip().split(':'))
        os.mknod(dev_path, stat.S_IFCHR | 0o600, os.makedev(major, minor))
        return True
    except (OSError, ValueError) as e:
        log.error(f"could not create {dev_path}: {e}")
        return False


def _ensure_hid_core(kernel, build, codename):
    """uhid needs the HID core; the Oasis 1 kernel ships without it.

    When the hid bus isn't registered, load a bundled hid.ko (hid-core +
    hid-input) so uhid's hid_* symbols resolve. No-op on kernels that
    already have HID built in.
    """
    if os.path.exists('/sys/bus/hid'):
        return
    ko = _find_bundled_ko(f"hid-{kernel}-{build}-{codename}.ko")
    if ko is None:
        return
    log.info(f"loading {os.path.basename(ko)} (HID core)")
    run(['/sbin/insmod', ko])


def _ensure_uhid():
    """Load bundled uhid.ko on Kindles whose stock kernel lacks CONFIG_UHID."""
    if os.path.exists('/dev/uhid'):
        return True
    codename = detect_codename()
    if not codename:
        return False
    build = _read_firmware_build()
    if not build:
        _log_missing_kmod(codename)
        return False
    kernel = os.uname().release
    expected = f"uhid-{kernel}-{build}-{codename}.ko"
    ko = _find_bundled_ko(expected)
    if ko is None:
        _log_missing_kmod(codename, expected)
        return False
    _ensure_hid_core(kernel, build, codename)
    log.info(f"loading {expected}")
    if not run(['/sbin/insmod', ko]):
        log.error("insmod failed")
        return False
    # devtmpfs kernels create /dev/uhid automatically; older ones (duet)
    # need the node created by hand from the misc device's sysfs entry.
    if not os.path.exists('/dev/uhid'):
        _create_dev_node('/sys/class/misc/uhid/dev', '/dev/uhid')
    return os.path.exists('/dev/uhid')


def prepare_bt():
    """Load uhid if needed, then prepare the chip. True if BT is ready."""
    _ensure_uhid()
    log.info("Preparing Bluetooth hardware...")
    return chip().prepare()
