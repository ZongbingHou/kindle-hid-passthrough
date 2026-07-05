#!/usr/bin/env python3
"""Watch powerd events and bracket BT around system sleep (issue #75)."""

import subprocess
import threading
import time

from logging_utils import log

SUSPEND_EVENTS = ('goingToScreenSaver', 'readyToSuspend')
RESUME_EVENTS = ('outOfScreenSaver', 'wakeupFromSuspend')

LIPC_WAIT_CMD = [
    'lipc-wait-event', '-m', '-s', '0', 'com.lab126.powerd',
    ','.join(SUSPEND_EVENTS + RESUME_EVENTS),
]
RESPAWN_DELAY = 5.0


class PowerMonitor:
    """Follow powerd events and drive the controller's system suspend hooks."""

    def __init__(self, controller):
        self.controller = controller
        self._proc = None
        self._stopping = False

    def start(self):
        threading.Thread(
            target=self._watch, name='power_monitor', daemon=True
        ).start()

    def stop(self):
        self._stopping = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _watch(self):
        while not self._stopping:
            try:
                self._proc = subprocess.Popen(
                    LIPC_WAIT_CMD,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except FileNotFoundError:
                log.warning("lipc-wait-event not found; suspend watch disabled")
                return
            for line in self._proc.stdout:
                event = line.split()[0] if line.strip() else ''
                if event in SUSPEND_EVENTS:
                    self.controller.on_system_suspend(event)
                elif event in RESUME_EVENTS:
                    self.controller.on_system_resume(event)
            self._proc.wait()
            if not self._stopping:
                log.warning(
                    f"lipc-wait-event exited ({self._proc.returncode}); respawning"
                )
                time.sleep(RESPAWN_DELAY)
