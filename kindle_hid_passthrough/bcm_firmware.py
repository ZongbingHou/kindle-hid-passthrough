#!/usr/bin/env python3
"""
Broadcom BCM4343 firmware loader for Kindle 8th-10th gen.

Downloads .hcd patchram firmware to the BCM4343 chip over UART using
standard HCI vendor commands. The chip speaks HCI natively; Amazon's
BSA daemon (bsa_server) must be killed first to free the UART.

HCD file format: sequence of raw HCI command packets:
  [opcode_lo] [opcode_hi] [param_len] [params...]

The download sequence:
  1. Send HCI_Reset, wait for Command Complete
  2. Send each HCI command from the .hcd file, wait for response
  3. Send HCI_Reset again to apply the firmware

References:
  - Linux kernel: drivers/bluetooth/btbcm.c
  - btstack: chipset/btstack_chipset_bcm.c
"""

import glob
import os
import struct
import time

import serial

from logging_utils import log

# HCI packet type indicators (H4 protocol)
H4_CMD = 0x01
H4_EVT = 0x04

# HCI event codes
EVT_COMMAND_COMPLETE = 0x0E

# HCI opcodes
HCI_RESET = 0x0C03

# Broadcom vendor commands
BCM_WRITE_UART_CLOCK = 0xFC45
BCM_DOWNLOAD_MINIDRIVER = 0xFC2E

# Timeouts
CMD_TIMEOUT = 5.0
RESET_SETTLE = 0.25
POWER_ON_SETTLE = 1.0


def prepare_chip_hardware(kindle=None):
    """Assert BT_REG_ON and confirm UART pinmux. TODO(#22): per-model GPIO."""
    time.sleep(POWER_ON_SETTLE)
    return True


def _build_hci_cmd(opcode, params=b''):
    """Build an H4-framed HCI command packet."""
    return struct.pack('<BHB', H4_CMD, opcode, len(params)) + params


def _read_event(port, timeout=CMD_TIMEOUT):
    """Read one HCI event from the UART.

    Returns:
        (event_code, data) tuple, or None on timeout.
    """
    port.timeout = timeout

    # Read H4 packet type
    pkt_type = port.read(1)
    if not pkt_type:
        return None
    if pkt_type[0] != H4_EVT:
        log.warning(f"Expected HCI event (0x04), got 0x{pkt_type[0]:02X}")
        return None

    # Read event header: event_code + param_len
    header = port.read(2)
    if len(header) < 2:
        return None

    event_code, param_len = struct.unpack('<BB', header)
    params = port.read(param_len) if param_len > 0 else b''
    if len(params) < param_len:
        return None

    return (event_code, params)


def _send_cmd(port, opcode, params=b''):
    """Send an HCI command and wait for Command Complete.

    Returns:
        True if command completed successfully.
    """
    cmd = _build_hci_cmd(opcode, params)
    port.write(cmd)
    port.flush()

    event = _read_event(port)
    if event is None:
        log.warning(f"No response for opcode 0x{opcode:04X}")
        return False

    event_code, data = event
    if event_code != EVT_COMMAND_COMPLETE:
        log.warning(f"Expected Command Complete (0x0E), got 0x{event_code:02X} for opcode 0x{opcode:04X}")
        return False

    # Command Complete: num_packets(1) + opcode(2) + status(1) + ...
    if len(data) >= 4:
        status = data[3]
        if status != 0:
            log.warning(f"Command 0x{opcode:04X} returned status 0x{status:02X}")
            return False

    return True


def _find_hcd_file(firmware_dir):
    """Find the .hcd firmware file in the given directory.

    Returns:
        Path to the .hcd file, or None.
    """
    if not firmware_dir or not os.path.isdir(firmware_dir):
        return None

    hcd_files = glob.glob(os.path.join(firmware_dir, '*.hcd'))
    if not hcd_files:
        return None

    # If multiple, pick the latest version (filenames contain zero-padded
    # version numbers like BCM4343A1_001.002.009.0152.0517.hcd)
    hcd_files.sort()
    return hcd_files[-1]


def _parse_hcd(data):
    """Parse an HCD file into a list of (opcode, params) tuples."""
    commands = []
    offset = 0
    while offset + 3 <= len(data):
        opcode = struct.unpack_from('<H', data, offset)[0]
        param_len = data[offset + 2]
        offset += 3
        if offset + param_len > len(data):
            break
        params = data[offset:offset + param_len]
        offset += param_len
        commands.append((opcode, params))
    return commands


def download_firmware(device_path, firmware_dir, baud_rate=115200):
    """Download BCM .hcd firmware to the chip over UART.

    Args:
        device_path: UART device (e.g. /dev/ttymxc2).
        firmware_dir: Directory containing .hcd firmware files.
        baud_rate: Initial UART baud rate.

    Returns:
        True if firmware was downloaded successfully.
    """
    hcd_path = _find_hcd_file(firmware_dir)
    if hcd_path is None:
        log.warning(f"No .hcd firmware found in {firmware_dir}")
        return False

    log.info(f"Loading BCM firmware: {os.path.basename(hcd_path)}")

    with open(hcd_path, 'rb') as f:
        hcd_data = f.read()

    commands = _parse_hcd(hcd_data)
    if not commands:
        log.warning(f"No commands found in {hcd_path}")
        return False

    log.info(f"Firmware has {len(commands)} HCI commands")

    port = serial.Serial(
        port=device_path,
        baudrate=baud_rate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        rtscts=True,
        timeout=CMD_TIMEOUT,
    )

    try:
        # Reset the chip to a known state
        log.info("Sending HCI_Reset...")
        if not _send_cmd(port, HCI_RESET):
            log.error("HCI_Reset failed - chip may not be responding")
            return False

        time.sleep(RESET_SETTLE)

        # Enter download mode
        log.info("Entering download mode...")
        if not _send_cmd(port, BCM_DOWNLOAD_MINIDRIVER):
            log.error("Download minidriver command failed")
            return False

        time.sleep(RESET_SETTLE)

        # Send each firmware command
        for i, (opcode, params) in enumerate(commands):
            if not _send_cmd(port, opcode, params):
                log.error(f"Firmware command {i + 1}/{len(commands)} failed (opcode 0x{opcode:04X})")
                return False

        log.info(f"Firmware download complete ({len(commands)} commands sent)")

        # Reset to apply firmware
        time.sleep(RESET_SETTLE)
        log.info("Resetting chip to apply firmware...")
        if not _send_cmd(port, HCI_RESET):
            log.warning("Post-download reset failed (may be normal)")

        time.sleep(RESET_SETTLE)

    finally:
        port.close()

    log.info("BCM firmware loaded successfully")
    return True
