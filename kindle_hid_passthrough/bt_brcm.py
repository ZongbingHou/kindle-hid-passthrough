#!/usr/bin/env python3
"""Broadcom BCM4343 (Kindle 8th-10th gen) BT over UART.

bsa_server owns the UART and does the timing-critical firmware download;
doing it cold ourselves wedges the kernel. So we let bsa warm the chip, then
SIGKILL it and take the running UART (warm handoff).
"""

import os
import signal
import subprocess
import threading
import time

from bt_chip import BtChip, free_device, run
from logging_utils import log

BT_DEV_WAKE_PATH = '/proc/bluetooth/sleep/btwake'
BT_SLEEP_PROTO_PATH = '/proc/bluetooth/sleep/proto'
BT_ENABLE_PATH = '/proc/bluetooth/btenable'
BTENABLE_LIPC = ['lipc-set-prop', 'com.lab126.btfd', 'BTenable', '1:1']
BSA_WARMUP_TIMEOUT = 12.0   # seconds to wait for bsa_server after BTenable
BSA_FIRMWARE_SETTLE = 2.0   # let bsa finish the .hcd download before we take over
BSA_WARMUP_RETRIES = 3      # btd-recovery attempts before giving up on warm-up
BSA_MIN_AGE = 5.0           # bsa_server younger than this may be mid .hcd download
POST_WAKE_SETTLE = 0.5      # let the chip settle after wake before first HCI cmd


def _pgrep_x(name):
    """Return PIDs whose process name exactly matches `name`."""
    try:
        r = subprocess.run(['pgrep', '-x', name],
                           capture_output=True, text=True, timeout=5)
        return [int(p) for p in r.stdout.split() if p.isdigit()]
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return []


def _pid_age(pid):
    """Seconds since `pid` started, or None if unreadable."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            starttime = int(f.read().rsplit(')', 1)[1].split()[19])
        with open('/proc/uptime') as f:
            uptime = float(f.read().split()[0])
        age = uptime - starttime / os.sysconf('SC_CLK_TCK')
        return age if age >= 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _wait_for_bsa():
    """Wait for bsa_server to appear (chip warmed). Returns True on success."""
    deadline = time.monotonic() + BSA_WARMUP_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if _pgrep_x('bsa_server'):
            log.info("bsa_server up; BCM chip warmed")
            time.sleep(BSA_FIRMWARE_SETTLE)  # let the .hcd download finish
            return True
    return False


def _recover_btd():
    """Unfreeze and restart btd so it can respawn bsa_server.

    A btd left frozen by a prior handoff makes `initctl restart` hang, so
    SIGCONT it first.
    """
    for pid in _pgrep_x('btd'):
        try:
            os.kill(pid, signal.SIGCONT)
        except OSError:
            pass
    run(['initctl', 'restart', 'btd'])
    time.sleep(2.0)
    run(BTENABLE_LIPC)


def _warm_up_chip():
    """Bring the chip up via Amazon's BT stack (it loads the firmware)."""
    pids = _pgrep_x('bsa_server')
    if pids:
        # killing bsa mid .hcd download leaves the chip half-programmed
        age = _pid_age(pids[0])
        if age is not None and age < BSA_MIN_AGE:
            log.info(f"bsa_server only {age:.1f}s old; waiting for firmware download")
            time.sleep(BSA_MIN_AGE - age)
        log.info("bsa_server already running; BCM chip is warm")
        return True

    log.info("Warming BCM chip via btfd (BTenable)...")
    run(BTENABLE_LIPC)
    if _wait_for_bsa():
        return True

    # bsa didn't come up: recover btd and retry. One restart can race a btd
    # that's still tearing down a previous session, so retry a few times.
    for attempt in range(1, BSA_WARMUP_RETRIES + 1):
        log.warning(
            f"bsa_server didn't start; recovering btd "
            f"(attempt {attempt}/{BSA_WARMUP_RETRIES})"
        )
        _recover_btd()
        if _wait_for_bsa():
            return True
    return False


def _handoff_from_bsa(device_path):
    """Freeze btd, SIGKILL bsa, free the UART. The chip stays warm."""
    for pid in _pgrep_x('btd'):
        try:
            os.kill(pid, signal.SIGSTOP)
            log.info(f"Froze btd ({pid}) so it won't respawn bsa_server")
        except OSError as e:
            log.warning(f"Could not SIGSTOP btd {pid}: {e}")

    for pid in _pgrep_x('bsa_server'):
        try:
            os.kill(pid, signal.SIGKILL)
            log.info(f"Killed bsa_server ({pid}); taking the UART")
        except OSError as e:
            log.warning(f"Could not kill bsa_server {pid}: {e}")

    time.sleep(0.5)
    free_device(device_path)
    time.sleep(0.3)


class BrcmChip(BtChip):
    survives_suspend = False

    def __init__(self, kindle):
        super().__init__(kindle)
        # Serialize prepare/power_off/ensure_powered so suspend and resume can't
        # half-power the chip; RLock since ensure_powered() calls prepare().
        self._power_lock = threading.RLock()
        self._warm = False  # True only after a successful handoff; reset by power_off

    def prepare(self):
        with self._power_lock:
            self._warm = False
            device_path = self.kindle.device_path
            if not os.path.exists(device_path):
                log.error(f"{device_path} does not exist")
                return False
            if not _warm_up_chip():
                log.error("Could not warm the BCM chip via bsa_server")
                return False
            _handoff_from_bsa(device_path)
            self._warm = True
            log.info(f"{device_path} ready (warm handoff complete; wake deferred to open)")
            return True

    def on_transport_open(self):
        try:
            with open(BT_DEV_WAKE_PATH, 'w') as f:
                f.write('0')
            log.info("BCM chip woken (dev_wake asserted)")
            time.sleep(0.3)
        except OSError as e:
            log.warning(f"Could not assert dev_wake: {e}")
        try:
            with open(BT_SLEEP_PROTO_PATH, 'w') as f:
                f.write('0')
            log.info("BCM bluesleep disabled (chip stays awake)")
            time.sleep(0.2)
        except OSError as e:
            log.warning(f"Could not disable bluesleep: {e}")
        # Settle after wake before the first HCI command, else HCI Reset times out.
        time.sleep(POST_WAKE_SETTLE)

    def power_off(self):
        with self._power_lock:
            self._warm = False
            try:
                if open(BT_ENABLE_PATH).read().strip() == '0':
                    return
                with open(BT_ENABLE_PATH, 'w') as f:
                    f.write('0')
                log.info("BCM chip powered off (btenable=0)")
            except OSError as e:
                log.warning(f"Could not power off BCM chip: {e}")
                return
            run(['initctl', 'restart', 'btd'])   # resets btfd BTstate -> clears the icon

    def on_hci_reset_timeout(self):
        log.warning("HCI Reset timed out on BCM; powering off for a clean re-warm")
        self.power_off()

    def ensure_powered(self):
        with self._power_lock:
            # btenable can read back 1 after btd respawns without a handoff, so
            # trust our own _warm flag to decide whether a re-warm is needed.
            if self._warm:
                if not _pgrep_x('bsa_server'):
                    return
                # bsa respawned behind our handoff: chip state unknown
                log.warning("bsa_server respawned after handoff; power-cycling chip")
                self.power_off()
            self.prepare()
