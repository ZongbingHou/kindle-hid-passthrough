#!/usr/bin/env python3
"""Bumble transport and device initialization."""

import asyncio

from bumble.device import Device
from bumble.hci import HCI_Reset_Command
from bumble.transport import open_transport

from config import config
from logging_utils import log

__all__ = ['create_bumble_device']


async def create_bumble_device(transport_spec=None, configure=None):
    """Open HCI transport, create a Bumble Device, reset, and power on.

    Args:
        transport_spec: HCI transport spec, defaults to config.transport.
        configure: Optional callback receiving the Device before HCI reset
            and power-on. Keystore, pairing config, and SSP/SC flags must
            be set here: power_on() applies them to the controller.

    Returns:
        (transport, device) tuple. The transport is closed before raising
        on any failure past the open, so no stpbt fd is leaked.
    """
    spec = transport_spec or config.transport
    if not spec:
        raise RuntimeError("No HCI transport available")

    log.info("Opening transport...")
    try:
        transport = await asyncio.wait_for(
            open_transport(spec),
            timeout=config.transport_timeout
        )
    except asyncio.TimeoutError:
        log.error(f"Transport open timed out after {config.transport_timeout}s")
        raise

    try:
        device = Device.with_hci(
            config.device_name,
            config.device_address,
            transport.source,
            transport.sink
        )

        if configure:
            configure(device)

        log.info("Sending HCI Reset...")
        try:
            await asyncio.wait_for(
                device.host.send_command(HCI_Reset_Command()),
                timeout=config.hci_reset_timeout
            )
            log.success("HCI Reset successful")
            await asyncio.sleep(0.2)
        except asyncio.TimeoutError:
            log.error("HCI Reset timed out")
            raise

        await device.power_on()
        log.success(f"Device powered on: {device.public_address}")
    except BaseException:
        try:
            await transport.close()
        except Exception:
            pass
        raise

    return transport, device
