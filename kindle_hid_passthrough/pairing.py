#!/usr/bin/env python3
"""Bluetooth pairing utilities — delegate, config, and keystore."""

from bumble.core import BT_BR_EDR_TRANSPORT
from bumble.keys import JsonKeyStore
from bumble.pairing import PairingConfig, PairingDelegate

from logging_utils import log

__all__ = ['AutoAcceptPairingDelegate', 'create_pairing_config', 'create_keystore']


class AutoAcceptPairingDelegate(PairingDelegate):
    """Pairing delegate that auto-accepts all pairing requests."""

    def __init__(self, io_capability=PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT):
        super().__init__(io_capability=io_capability)

    async def accept(self):
        log.success("Pairing request received - accepting")
        return True

    async def compare_numbers(self, number, digits):
        log.warning(f"Confirm number: {number:0{digits}}")
        log.warning("Auto-accepting (press Ctrl+C to cancel)")
        return True

    async def get_number(self):
        return 0

    async def display_number(self, number, digits):
        log.info(f"Display PIN: {number:0{digits}}")


def create_pairing_config(conn=None) -> PairingConfig:
    """Create pairing configuration; relaxed for Classic, strict for BLE."""
    transport = getattr(conn, 'transport', None) if conn is not None else None
    if transport == BT_BR_EDR_TRANSPORT:
        log.info("[Pairing] Classic config: sc=False, mitm=False, NoInputNoOutput")
        return PairingConfig(
            sc=False,
            mitm=False,
            bonding=True,
            delegate=AutoAcceptPairingDelegate(
                io_capability=PairingDelegate.NO_OUTPUT_NO_INPUT
            ),
        )

    return PairingConfig(
        sc=True,
        mitm=True,
        bonding=True,
        delegate=AutoAcceptPairingDelegate(),
    )


def create_keystore(path: str) -> JsonKeyStore:
    """Create a JSON-based key store for bonding keys."""
    return JsonKeyStore(namespace=None, filename=path)
