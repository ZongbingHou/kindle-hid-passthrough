#!/usr/bin/env python3
"""Bluetooth pairing utilities — delegate, config, and keystore."""

from bumble.keys import JsonKeyStore
from bumble.pairing import PairingConfig, PairingDelegate

from logging_utils import log

__all__ = ['AutoAcceptPairingDelegate', 'create_pairing_config', 'create_keystore']


class AutoAcceptPairingDelegate(PairingDelegate):
    """Pairing delegate that auto-accepts all pairing requests."""

    def __init__(self):
        super().__init__(
            io_capability=PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT
        )

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


def create_pairing_config() -> PairingConfig:
    """Create pairing configuration with secure defaults."""
    return PairingConfig(
        sc=True,
        mitm=True,
        bonding=True,
        delegate=AutoAcceptPairingDelegate(),
    )


def create_keystore(path: str) -> JsonKeyStore:
    """Create a JSON-based key store for bonding keys."""
    return JsonKeyStore(namespace=None, filename=path)
