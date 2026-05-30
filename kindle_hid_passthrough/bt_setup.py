#!/usr/bin/env python3
"""
Bluetooth hardware setup for Kindle.

Ensures the BT kernel module is loaded and any process holding the HCI
device is evicted before opening the transport.

Auto-detects kernel version and module paths. Override via config.ini:

    [bluetooth]
    module_patterns = wmt_cdev_bt.ko, bt_drv.ko
    settle_time = 0.5

"""

import glob
import os
import re
import signal
import subprocess
import time

from kindle_detect import detect_codename, detect_kindle
from logging_utils import log

# Known BT kernel module patterns across Kindle versions
DEFAULT_MODULE_PATTERNS = [
    'wmt_cdev_bt.ko',   # MediaTek (PW4/5, Kindle 10/11, Scribe)
    'bt_drv.ko',         # Older Freescale/NXP Kindles
]

BUNDLED_MODULES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'docs', 'modules'
)
VERSION_TXT = '/etc/version.txt'


def _run(cmd, **kwargs):
    """Run a command silently, return success."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10, **kwargs)
        return r.returncode == 0
    except Exception:
        return False


def _find_bt_module(patterns=None):
    """Find the BT kernel module path for the running kernel.

    Returns:
        Module path string, or None if not found.
    """
    patterns = patterns or DEFAULT_MODULE_PATTERNS

    try:
        uname = os.uname().release
    except Exception:
        return None

    base = f'/lib/modules/{uname}/extra'

    for pattern in patterns:
        matches = glob.glob(f'{base}/{pattern}')
        if matches:
            return matches[0]
        # Also check subdirectories
        matches = glob.glob(f'{base}/**/{pattern}', recursive=True)
        if matches:
            return matches[0]

    return None


def _is_module_loaded(module_path):
    """Check if a kernel module is already loaded."""
    mod_name = os.path.basename(module_path).replace('.ko', '')
    try:
        with open('/proc/modules', 'r') as f:
            for line in f:
                if line.split()[0] == mod_name:
                    return True
    except Exception:
        pass
    return False


def _free_device(device_path):
    """Kill whatever userspace process is holding the BT device.

    Uses fuser(1), which queries the kernel for open file descriptors
    on the device. This avoids hardcoding Amazon's process names
    (bluetoothd, acsbtfd, btif_rxd, etc.) which differ across
    firmwares. Kernel threads don't appear in fuser output, which is
    correct since they can't be killed from userspace anyway.

    Returns:
        True if fuser ran (regardless of whether anything was killed).
    """
    try:
        r = subprocess.run(['fuser', '-k', device_path],
                           capture_output=True, timeout=5)
        # fuser returns 0 if a process was found+signalled, 1 if no
        # process held the file. Both are success for our purposes.
        if r.returncode == 0:
            holders = r.stderr.decode(errors='replace').strip()
            log.info(f"Evicted holders of {device_path}: {holders}")
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning(f"fuser unavailable or timed out: {e}")
        return False


def _read_firmware_build():
    try:
        with open(VERSION_TXT) as f:
            line = f.readline()
    except OSError:
        return None
    m = re.search(r'-(\d+)\s*$', line.strip())
    return m.group(1) if m else None


def _ensure_uhid():
    """Load bundled uhid.ko on Kindles whose stock kernel lacks CONFIG_UHID."""
    if os.path.exists('/dev/uhid'):
        return True
    codename = detect_codename()
    if not codename:
        return False
    build = _read_firmware_build()
    if not build:
        log.error(f"could not read build from {VERSION_TXT}")
        return False
    kernel = os.uname().release
    expected = f"uhid-{kernel}-{build}-{codename}.ko"
    ko = os.path.join(BUNDLED_MODULES_DIR, expected)
    if not os.path.exists(ko):
        log.error(f"no bundled uhid.ko matching {expected}")
        log.error("please file an issue with /etc/version.txt output")
        return False
    log.info(f"loading {expected}")
    if not _run(['/sbin/insmod', ko]):
        log.error("insmod failed")
        return False
    return os.path.exists('/dev/uhid')


def _kill_holders_via_proc(device_path):
    """Kill processes holding device_path by scanning /proc directly.

    Fallback for when fuser(1) is missing or didn't catch the holder.
    Walks every /proc/<pid>/fd, resolves each symlink, and SIGKILLs any
    process (other than ourselves) with the device open.

    Returns:
        Number of processes signalled.
    """
    my_pid = os.getpid()
    killed = 0
    try:
        pids = [e for e in os.listdir('/proc') if e.isdigit()]
    except OSError:
        return 0

    for pid in pids:
        if int(pid) == my_pid:
            continue
        fd_dir = f'/proc/{pid}/fd'
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # process exited or not readable
        for fd in fds:
            try:
                target = os.readlink(f'{fd_dir}/{fd}')
            except OSError:
                continue
            if target == device_path:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    killed += 1
                    log.info(f"Killed PID {pid} holding {device_path}")
                except OSError as e:
                    log.warning(f"Could not kill PID {pid}: {e}")
                break  # one match per process is enough

    return killed


def _is_device_free(device_path):
    """Check if the BT device can be opened."""
    try:
        fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
        os.close(fd)
        return True
    except OSError:
        return False


def prepare_bt(transport_spec=None, module_patterns=None, settle_time=0.5):
    """Prepare Bluetooth hardware for use.

    1. Load BT kernel module if not already loaded
    2. Evict whatever process is holding the HCI device (fuser, then a
       direct /proc scan if fuser is unavailable or comes up short)
    3. Wait for device to settle

    Uses auto-detected Kindle hardware defaults when module_patterns
    is not specified and not overridden in config.ini.

    Args:
        transport_spec: Transport string (e.g. 'file:/dev/stpbt') to
                       extract device path. If None, uses /dev/stpbt.
        module_patterns: List of module filename patterns to search for.
        settle_time: Seconds to wait after evicting holders.

    Returns:
        True if BT device is ready.
    """
    # Load bundled uhid.ko first; no-op on Kindles where /dev/uhid already exists
    _ensure_uhid()

    # Use auto-detected Kindle defaults when not explicitly provided
    kindle = detect_kindle()

    # Extract device path from transport spec, or use detected default
    device_path = '/dev/stpbt'
    if transport_spec and transport_spec.startswith('file:'):
        device_path = transport_spec[5:]
    elif kindle:
        device_path = kindle.device_path

    if module_patterns is None and kindle:
        module_patterns = [kindle.kernel_module]

    log.info("Preparing Bluetooth hardware...")

    # Step 1: Load kernel module
    module_path = _find_bt_module(module_patterns)
    if module_path:
        if _is_module_loaded(module_path):
            log.info(f"BT module already loaded: {os.path.basename(module_path)}")
        else:
            log.info(f"Loading BT module: {module_path}")
            if _run(['/sbin/insmod', module_path]):
                log.info("BT module loaded")
                time.sleep(0.5)  # wait for /dev node to appear
            else:
                log.warning(f"Failed to load {module_path} (may need root)")
    else:
        log.info("No BT kernel module found (may already be built-in)")

    # Step 2: Check if device is available
    if not os.path.exists(device_path):
        log.warning(f"{device_path} does not exist")
        return False

    if _is_device_free(device_path):
        log.info(f"{device_path} is available")
        return True

    # Step 3: Device is busy - evict whoever holds it, first via fuser
    log.info(f"{device_path} is busy, evicting holder...")
    if _free_device(device_path) and settle_time > 0:
        time.sleep(settle_time)

    if _is_device_free(device_path):
        log.info(f"{device_path} is now available")
        return True

    # Step 4: fuser came up short (missing or didn't catch the holder).
    # Scan /proc ourselves and kill whoever has the device open.
    log.warning(f"{device_path} still busy, scanning /proc for holders...")
    if _kill_holders_via_proc(device_path) and settle_time > 0:
        time.sleep(settle_time)

    if _is_device_free(device_path):
        log.info(f"{device_path} is now available")
        return True

    # Last resort: try again with a longer wait
    log.warning(f"{device_path} still busy, waiting 2s...")
    time.sleep(2.0)

    if _is_device_free(device_path):
        log.info(f"{device_path} is now available")
        return True

    log.warning(f"{device_path} still busy after cleanup")
    return False
