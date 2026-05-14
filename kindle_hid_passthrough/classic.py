#!/usr/bin/env python3
"""Classic Bluetooth HID handler mixin for HIDHost."""

import asyncio
from typing import List

from bumble.core import BT_BR_EDR_TRANSPORT, BT_HUMAN_INTERFACE_DEVICE_SERVICE, InvalidStateError
from bumble.hci import (
    Address,
    HCI_Write_Scan_Enable_Command,
)
from bumble.hid import HID_CONTROL_PSM, HID_INTERRUPT_PSM
from bumble.hid import Host as BumbleHIDHost
from bumble.sdp import Client as SDPClient

from config import Protocol, normalize_addr
from logging_utils import log

FALLBACK_HID_DESCRIPTOR = bytes([
    0x05, 0x01, 0x09, 0x05, 0xa1, 0x01, 0x85, 0x01,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,
    0x16, 0x00, 0x00, 0x26, 0xff, 0xff, 0x75, 0x10, 0x95, 0x04, 0x81, 0x02,
    0x05, 0x02, 0x09, 0xc5, 0x09, 0xc4,
    0x16, 0x00, 0x00, 0x26, 0xff, 0x03, 0x75, 0x10, 0x95, 0x02, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x39, 0x15, 0x01, 0x25, 0x08,
    0x35, 0x00, 0x46, 0x3b, 0x01, 0x65, 0x14, 0x75, 0x08, 0x95, 0x01, 0x81, 0x42,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x10,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x10, 0x81, 0x02,
    0xc0,
])


class ClassicMixin:
    """Classic Bluetooth methods for HIDHost."""

    async def _run_classic_handler(self):
        """Handle Classic Bluetooth connections."""
        if hasattr(self, '_classic_connection_listener') and self._classic_connection_listener:
            try:
                self.device.remove_listener('connection', self._classic_connection_listener)
            except Exception:
                pass
            self._classic_connection_listener = None

        self.hid_host = BumbleHIDHost(self.device)
        self.hid_host.on(BumbleHIDHost.EVENT_INTERRUPT_DATA, self._on_classic_interrupt_data)
        self.hid_host.on(BumbleHIDHost.EVENT_VIRTUAL_CABLE_UNPLUG, self._on_virtual_cable_unplug)
        log.info(f"[Classic] HID Host ready (PSM 0x{HID_CONTROL_PSM:04X}, 0x{HID_INTERRUPT_PSM:04X})")

        log.info("[Classic] Enabling Page Scan...")
        await self.device.host.send_command(
            HCI_Write_Scan_Enable_Command(scan_enable=0x02),
            check_result=True
        )

        async def on_classic_connection(connection):
            if self._connection_future.done():
                log.info("[Classic] Connection received but another protocol won")
                try:
                    await connection.disconnect()
                except Exception:
                    pass
                return

            if not self.hid_host:
                log.warning("[Classic] Connection received but hid_host not ready, ignoring")
                try:
                    await connection.disconnect()
                except Exception:
                    pass
                return

            addr_str = str(connection.peer_address)
            log.info(f"[Classic] Device connected: {self._format_device(addr_str)}")

            if not self._is_classic_allowed(addr_str):
                log.warning(f"[Classic] Rejecting {addr_str} (not allowed)")
                try:
                    await connection.disconnect()
                except Exception:
                    pass
                return

            self.connection = connection
            self.current_device_address = addr_str
            self.connected_protocol = Protocol.CLASSIC
            connection.on('disconnection', self._on_disconnection)

            self.hid_host.on_device_connection(connection)

            auth_event = asyncio.Event()

            def on_auth():
                log.success("[Classic] Device authenticated us")
                auth_event.set()

            def on_auth_fail(error):
                log.warning(f"[Classic] Auth failed: {error}")
                auth_event.set()

            connection.on('connection_authentication', on_auth)
            connection.on('connection_authentication_failure', on_auth_fail)

            log.info("[Classic] Waiting for device authentication...")
            try:
                await asyncio.wait_for(auth_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("[Classic] No auth request from device, continuing...")

            try:
                connection.remove_listener('connection_authentication', on_auth)
                connection.remove_listener('connection_authentication_failure', on_auth_fail)
            except Exception:
                pass

            if self._disconnection_event.is_set():
                log.warning("[Classic] Connection lost during authentication")
                return

            log.info("[Classic] Waiting for HID channels...")
            for _ in range(30):
                if self._disconnection_event.is_set():
                    log.warning("[Classic] Connection lost while waiting for HID channels")
                    return
                if self.hid_host.l2cap_intr_channel and self.hid_host.l2cap_ctrl_channel:
                    log.success("[Classic] HID channels opened")
                    break
                await asyncio.sleep(0.1)

            if self._disconnection_event.is_set():
                log.warning("[Classic] Connection lost during HID setup")
                return

            if not self.hid_host.l2cap_ctrl_channel:
                try:
                    await asyncio.wait_for(self.hid_host.connect_control_channel(), timeout=5.0)
                except Exception:
                    pass

            if not self.hid_host.l2cap_intr_channel:
                try:
                    await asyncio.wait_for(self.hid_host.connect_interrupt_channel(), timeout=5.0)
                except Exception:
                    pass

            if self._disconnection_event.is_set():
                log.warning("[Classic] Connection lost during channel setup")
                return

            if not self.hid_host.l2cap_intr_channel:
                log.warning("[Classic] HID interrupt channel failed to connect")
                return

            if not self._connection_future.done():
                self._connection_future.set_result(connection)

        def on_connection_event(connection):
            is_classic = (hasattr(connection, 'transport')
                          and connection.transport == BT_BR_EDR_TRANSPORT) \
                          or not hasattr(connection, 'transport')
            if not is_classic:
                return
            task = asyncio.create_task(on_classic_connection(connection))
            self._connection_tasks.add(task)
            task.add_done_callback(self._connection_tasks.discard)

        self._classic_connection_listener = on_connection_event
        self.device.on('connection', on_connection_event)

        active_addresses = [d.address for d in self.classic_devices if d.address != '*']
        if active_addresses:
            await self._classic_active_connect_loop(active_addresses)

    def _is_classic_allowed(self, addr_str: str) -> bool:
        """Check if Classic address is allowed."""
        norm_addr = normalize_addr(addr_str)

        for dev in self.classic_devices:
            if dev.address == '*':
                return True
            if dev.address == norm_addr:
                return True

        if norm_addr in self._keystore_addresses:
            return True

        return False

    async def _classic_active_connect_loop(self, addresses: List[str]):
        """Actively try to connect to Classic devices."""
        log.info(f"[Classic] Active: {len(addresses)} device(s)")
        await asyncio.sleep(self.ACTIVE_DELAY)

        attempt = 0
        while not self._connection_future.done():
            attempt += 1
            for addr in addresses:
                if self._connection_future.done():
                    return

                log.info(f"[Classic] Attempt {attempt}: {self._format_device(addr)}")

                try:
                    target = Address(addr, Address.PUBLIC_DEVICE_ADDRESS)
                    connect_task = asyncio.create_task(
                        self.device.connect(target, transport=BT_BR_EDR_TRANSPORT)
                    )

                    for _ in range(self.ACTIVE_CONNECT_TIMEOUT):
                        if self._connection_future.done():
                            connect_task.cancel()
                            return

                        done, _ = await asyncio.wait([connect_task], timeout=0.5)
                        if done:
                            break

                    if not connect_task.done():
                        log.info(f"[Classic] {addr} timed out")
                        connect_task.cancel()
                        try:
                            await connect_task
                        except asyncio.CancelledError:
                            pass
                        await asyncio.sleep(3.0)
                        continue

                    await connect_task

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if "DISALLOWED" in str(e) or "PENDING" in str(e):
                        log.warning("[Classic] HCI busy, waiting...")
                        await asyncio.sleep(5.0)
                    else:
                        log.info(f"[Classic] Connect failed: {e}")
                        await asyncio.sleep(2.0)

            if not self._connection_future.done():
                await asyncio.sleep(self.ACTIVE_RETRY_INTERVAL)

    def _finalize_classic_hid(self):
        """Apply fallback descriptor if needed and create UHID."""
        if not self.report_map:
            self.report_map = FALLBACK_HID_DESCRIPTOR
            log.warning("[Classic] Using fallback descriptor")
        self._create_uhid_device()

    async def _handle_classic_connection(self):
        """Finalize Classic connection setup."""
        if not self.hid_host.l2cap_intr_channel:
            raise InvalidStateError("HID interrupt channel not connected")

        if not self._load_cached_descriptor():
            await self._query_classic_sdp()

        self._finalize_classic_hid()

    def _parse_hid_descriptor_list(self, data_element):
        """Parse HID Descriptor List from SDP."""
        try:
            if hasattr(data_element, 'value'):
                data_element = data_element.value

            if isinstance(data_element, (list, tuple)):
                for descriptor in data_element:
                    if hasattr(descriptor, 'value'):
                        descriptor = descriptor.value

                    if isinstance(descriptor, (list, tuple)) and len(descriptor) >= 2:
                        desc_type = descriptor[0]
                        desc_data = descriptor[1]

                        if hasattr(desc_type, 'value'):
                            desc_type = desc_type.value

                        if desc_type == 0x22:  # Report Descriptor
                            if hasattr(desc_data, 'value'):
                                desc_data = desc_data.value

                            if isinstance(desc_data, bytes):
                                self.report_map = desc_data
                            elif isinstance(desc_data, (list, tuple)):
                                self.report_map = bytes(desc_data)

                            log.success(f"[Classic] Got descriptor: {len(self.report_map)} bytes")
                            return
        except Exception as e:
            log.warning(f"[Classic] Failed to parse descriptor: {e}")

    def _on_classic_interrupt_data(self, pdu: bytes):
        """Handle Classic HID report."""
        if len(pdu) < 1:
            return
        self._forward_report(pdu[1:])

    def _on_virtual_cable_unplug(self):
        """Handle virtual cable unplug."""
        log.warning("[Classic] Virtual cable unplugged")
        self._disconnection_event.set()
