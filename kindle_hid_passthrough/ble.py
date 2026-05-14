#!/usr/bin/env python3
"""BLE HID handler mixin for HIDHost."""

import asyncio

from bumble.core import BT_LE_TRANSPORT, InvalidStateError
from bumble.device import Device, Peer
from bumble.gatt import (
    GATT_DEVICE_NAME_CHARACTERISTIC,
    GATT_GENERIC_ACCESS_SERVICE,
    GATT_HID_CONTROL_POINT_CHARACTERISTIC,
    GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
    GATT_PROTOCOL_MODE_CHARACTERISTIC,
    GATT_REPORT_CHARACTERISTIC,
    GATT_REPORT_MAP_CHARACTERISTIC,
    GATT_REPORT_REFERENCE_DESCRIPTOR,
)
from bumble.hci import (
    Address,
    HCI_LE_Add_Device_To_Filter_Accept_List_Command,
    HCI_LE_Clear_Filter_Accept_List_Command,
    HCI_LE_Create_Connection_Cancel_Command,
    HCI_LE_Create_Connection_Command,
    OwnAddressType,
)

from config import Protocol, config, normalize_addr
from logging_utils import log

HID_REPORT_TYPE_INPUT = 1


class BLEMixin:
    """BLE methods for HIDHost."""

    async def _run_ble_handler(self):
        """Handle BLE connections."""
        known_addresses = [dev.address for dev in self.ble_devices if dev.address != '*']
        has_wildcard = any(dev.address == '*' for dev in self.ble_devices)

        if known_addresses:
            await self._run_ble_accept_list_handler(known_addresses)
        elif has_wildcard:
            await self._run_ble_scan_handler(set())

    async def _run_ble_accept_list_handler(self, addresses: list):
        """Wait for BLE connections using the filter accept list."""
        await self.device.send_command(
            HCI_LE_Clear_Filter_Accept_List_Command(), check_result=True)

        for addr_str in addresses:
            target = Address(addr_str)
            await self.device.send_command(
                HCI_LE_Add_Device_To_Filter_Accept_List_Command(
                    address_type=target.address_type,
                    address=target,
                ), check_result=True)

        log.info(f"[BLE] Waiting for {len(addresses)} device(s) (accept list)")

        pending = asyncio.get_running_loop().create_future()

        def on_connection(connection):
            if connection.transport == BT_LE_TRANSPORT and not pending.done():
                pending.set_result(connection)

        def on_failure(error):
            if not pending.done():
                pending.set_exception(error)

        self.device.on(Device.EVENT_CONNECTION, on_connection)
        self.device.on(Device.EVENT_CONNECTION_FAILURE, on_failure)

        try:
            self.device.connect_own_address_type = OwnAddressType.PUBLIC
            self.device.le_connecting = True

            await self.device.send_command(
                HCI_LE_Create_Connection_Command(
                    le_scan_interval=96,
                    le_scan_window=96,
                    initiator_filter_policy=1,
                    peer_address_type=0,
                    peer_address=Address.ANY,
                    own_address_type=OwnAddressType.PUBLIC,
                    connection_interval_min=12,
                    connection_interval_max=24,
                    max_latency=0,
                    supervision_timeout=72,
                    min_ce_length=0,
                    max_ce_length=0,
                ), check_result=True)

            connection = await asyncio.shield(pending)

            if self._connection_future.done():
                await connection.disconnect()
                return

            addr_str = str(connection.peer_address)
            log.info(f"[BLE] Device connected: {self._format_device(addr_str)}")

            self.connection = connection
            self.peer = Peer(connection)
            self.current_device_address = addr_str
            self.connected_protocol = Protocol.BLE
            connection.on('disconnection', self._on_disconnection)

            await self._ble_restore_or_pair()

            if not self._connection_future.done():
                self._connection_future.set_result(connection)

        except asyncio.CancelledError:
            try:
                await self.device.send_command(
                    HCI_LE_Create_Connection_Cancel_Command())
            except Exception:
                pass
            raise
        except Exception as e:
            log.warning(f"[BLE] Accept list connection failed: {e}")
            try:
                await self.device.send_command(
                    HCI_LE_Create_Connection_Cancel_Command())
            except Exception:
                pass
        finally:
            self.device.le_connecting = False
            self.device.remove_listener(Device.EVENT_CONNECTION, on_connection)
            self.device.remove_listener(Device.EVENT_CONNECTION_FAILURE, on_failure)

    async def _run_ble_scan_handler(self, target_addresses: set):
        """Fallback BLE handler using active scanning for discovery."""
        log.info("[BLE] Scanning for devices...")

        while not self._connection_future.done():
            found_device = None

            def on_advertisement(advertisement):
                nonlocal found_device
                if self._connection_future.done():
                    return

                addr = normalize_addr(str(advertisement.address))
                if not target_addresses or addr in target_addresses:
                    found_device = advertisement
                    log.info(f"[BLE] Found target: {addr}")

            self.device.on('advertisement', on_advertisement)

            try:
                await self.device.start_scanning()
                for _ in range(20):
                    if found_device or self._connection_future.done():
                        break
                    await asyncio.sleep(0.5)
                await self.device.stop_scanning()
            except Exception as e:
                log.warning(f"[BLE] Scan error: {e}")
            finally:
                self.device.remove_listener('advertisement', on_advertisement)

            if self._connection_future.done():
                return

            if found_device:
                max_attempts = 2
                for attempt in range(1, max_attempts + 1):
                    try:
                        log.info(f"[BLE] Connecting to {found_device.address} (Attempt {attempt}/{max_attempts})...")
                        self.connection = await self.device.connect(
                            found_device.address,
                            own_address_type=OwnAddressType.PUBLIC,
                            timeout=config.connect_timeout,
                        )

                        if self._connection_future.done():
                            await self.connection.disconnect()
                            return

                        self.peer = Peer(self.connection)
                        self.current_device_address = str(found_device.address)
                        self.connected_protocol = Protocol.BLE
                        self.connection.on('disconnection', self._on_disconnection)

                        await self._ble_restore_or_pair()

                        if not self._connection_future.done():
                            self._connection_future.set_result(self.connection)
                        return

                    except Exception as e:
                        log.warning(f"[BLE] Connect attempt {attempt} failed: {e}")
                        if attempt < max_attempts:
                            await asyncio.sleep(2.0)

            if not self._connection_future.done():
                await asyncio.sleep(3.0)

    async def _ble_restore_or_pair(self):
        """Restore BLE bonding or initiate new pairing."""
        if self.connection.is_encrypted:
            log.info("[BLE] Connection already encrypted")
            return

        if self.device.keystore:
            try:
                keys = await self.device.keystore.get(str(self.connection.peer_address))
                if keys:
                    log.info("[BLE] Restoring bonding...")
                    await self.connection.encrypt()
                    log.success("[BLE] Bonding restored")
                    return
            except Exception as e:
                log.warning(f"[BLE] Bonding restore failed: {e}")

        log.info("[BLE] Initiating pairing...")
        await self.connection.pair()
        log.success("[BLE] Pairing complete")

    async def _setup_ble_hid(self):
        """Discover reports, create UHID, subscribe. Common to connect and post-pair."""
        if not self.hid_reports:
            await self._discover_ble_hid_service(process_reports=True)
        if not self.report_map:
            raise InvalidStateError("[BLE] No report descriptor available")
        self._create_uhid_device()
        await self._subscribe_to_ble_reports()
        await self._ble_activate_hid_service()

    async def _handle_ble_connection(self):
        """Finalize BLE connection setup."""
        self._load_cached_descriptor()
        await self._setup_ble_hid()

    async def _read_ble_device_name(self):
        """Read BLE device name from Generic Access Service."""
        try:
            for service in self.peer.services:
                if service.uuid == GATT_GENERIC_ACCESS_SERVICE:
                    await self.peer.discover_characteristics(service=service)
                    for char in service.characteristics:
                        if char.uuid == GATT_DEVICE_NAME_CHARACTERISTIC:
                            value = await self.peer.read_value(char)
                            self.device_name = bytes(value).decode('utf-8', errors='replace')
                            log.info(f"[BLE] Device name: {self.device_name}")
                            return
        except Exception as e:
            log.warning(f"[BLE] Could not read device name: {e}")

    async def _process_ble_report_char(self, char):
        """Process a BLE Report characteristic."""
        await self.peer.discover_descriptors(characteristic=char)

        report_id = 0
        report_type = HID_REPORT_TYPE_INPUT

        for desc in char.descriptors:
            if desc.type == GATT_REPORT_REFERENCE_DESCRIPTOR:
                try:
                    ref = await self.peer.read_value(desc)
                    if len(ref) >= 2:
                        report_id = ref[0]
                        report_type = ref[1]
                except Exception:
                    pass

        if report_type == HID_REPORT_TYPE_INPUT:
            self.hid_reports[report_id] = char
            log.info(f"[BLE] Found input report {report_id}")

    async def _subscribe_to_ble_reports(self):
        """Subscribe to BLE HID input report notifications."""
        for report_id, char in self.hid_reports.items():
            try:
                def make_callback(rid):
                    return lambda value: self._on_ble_hid_report(value, rid)

                await self.peer.subscribe(char, make_callback(report_id))
                log.success(f"[BLE] Subscribed to report {report_id}")
            except Exception as e:
                log.warning(f"[BLE] Failed to subscribe to report {report_id}: {e}")

    async def _ble_activate_hid_service(self):
        """Write Exit Suspend to HID Control Point and read Protocol Mode."""
        if not self.peer:
            log.warning("[BLE] No peer for HID activation")
            return

        hid_services = [s for s in self.peer.services if s.uuid == GATT_HUMAN_INTERFACE_DEVICE_SERVICE]
        if not hid_services:
            log.warning("[BLE] No HID service found for activation")
            return

        hid_service = hid_services[0]
        if not hid_service.characteristics:
            log.info("[BLE] Discovering characteristics for HID activation...")
            await self.peer.discover_characteristics(service=hid_service)

        found_cp = False
        for char in hid_service.characteristics:
            if char.uuid == GATT_HID_CONTROL_POINT_CHARACTERISTIC:
                found_cp = True
                try:
                    await self.peer.write_value(char, bytes([0x01]), with_response=False)
                    log.info("[BLE] Wrote Exit Suspend to HID Control Point")
                except Exception as e:
                    log.warning(f"[BLE] Failed to write HID Control Point: {e}")

            elif char.uuid == GATT_PROTOCOL_MODE_CHARACTERISTIC:
                try:
                    value = await self.peer.read_value(char)
                    mode = "Report" if bytes(value) == b'\x01' else "Boot"
                    log.info(f"[BLE] Protocol Mode: {mode}")
                except Exception as e:
                    log.warning(f"[BLE] Failed to read Protocol Mode: {e}")

        if not found_cp:
            log.info(f"[BLE] No HID Control Point characteristic (found {len(hid_service.characteristics)} chars)")

    def _on_ble_hid_report(self, value, report_id):
        """Handle BLE HID report."""
        self._forward_report(bytes([report_id]) + bytes(value))
