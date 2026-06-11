#!/usr/bin/env python3
"""HID Host — runs BLE + Classic handlers on a single Bumble device."""

import asyncio
from dataclasses import dataclass
from typing import List, Optional

from bumble.core import BT_BR_EDR_TRANSPORT, BT_HUMAN_INTERFACE_DEVICE_SERVICE, InvalidStateError, TimeoutError as BumbleTimeoutError
from bumble.device import Peer
from bumble.gatt import (
    GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
    GATT_REPORT_CHARACTERISTIC,
    GATT_REPORT_MAP_CHARACTERISTIC,
)
from bumble.hci import Address, HCI_LE_SET_PRIVACY_MODE_COMMAND, HCI_LE_Set_Privacy_Mode_Command, HCI_Write_Class_Of_Device_Command, HCI_Write_Local_Name_Command, OwnAddressType
from bumble.hid import Host as BumbleHIDHost
from bumble.sdp import Client as SDPClient

from ble import BLEMixin
from classic import ClassicMixin
from config import Protocol, config, get_version, normalize_addr
from device_cache import DeviceCache
from logging_utils import log
from pairing import create_keystore, create_pairing_config
from transport import create_bumble_device
from uhid_handler import Bus, UHIDDevice, strip_digitizer_collections

__all__ = ['HIDHost']


@dataclass
class DeviceConfig:
    """Device configuration from devices.conf."""
    address: str
    protocol: Protocol
    name: Optional[str] = None


class HIDHost(ClassicMixin, BLEMixin):
    """HID Host supporting both BLE and Classic Bluetooth.

    Protocol-specific handlers live in ClassicMixin and BLEMixin.
    This class owns init, start, run, pairing dispatch, and cleanup.
    """

    PROTOCOL_NAME = "HID"

    ACTIVE_DELAY = 2.0
    ACTIVE_RETRY_INTERVAL = 5.0
    ACTIVE_CONNECT_TIMEOUT = 10

    def __init__(self, transport_spec: str = None):
        self.transport_spec = transport_spec or config.transport
        self.transport = None
        self.device = None
        self.connection = None
        self.peer = None

        self.hid_host = None
        self.connected_protocol = None

        self._connection_tasks: set = set()

        self.current_device_address = None
        self.device_name = None
        self.report_map: Optional[bytes] = None
        self.hid_reports = {}

        self.classic_devices: List[DeviceConfig] = []
        self.ble_devices: List[DeviceConfig] = []
        self._keystore_addresses: set = set()
        self._keystore_address_types: dict = {}

        self.keystore = create_keystore(config.pairing_keys_file)
        self.device_cache = DeviceCache(config.cache_dir)

        self.uhid_device = None

        self._disconnection_event = None
        self._connection_future = None
        self._last_report = None
        self._auth_failure_address = None

    @property
    def connection_state(self) -> dict:
        """Current connection state as a dict for API consumers."""
        if not self._is_connection_alive():
            return {"connected": False}

        state = {
            "connected": True,
            "address": normalize_addr(self.current_device_address) if self.current_device_address else None,
            "protocol": self.connected_protocol.value if self.connected_protocol else None,
            "name": self.device_name,
        }
        if self.uhid_device:
            state["uhid_name"] = self.uhid_device.name
            if self.uhid_device.input_paths:
                state["input_paths"] = self.uhid_device.input_paths
        if self.report_map:
            state["descriptor_size"] = len(self.report_map)
        return state

    def _parse_devices(self):
        """Parse devices from config and group by protocol."""
        devices = config.get_all_devices()
        self.classic_devices = []
        self.ble_devices = []

        for addr, protocol, name in devices:
            dev = DeviceConfig(address=addr, protocol=protocol, name=name)
            if protocol == Protocol.CLASSIC:
                self.classic_devices.append(dev)
            else:
                self.ble_devices.append(dev)

        log.info(f"Devices: {len(self.classic_devices)} Classic, {len(self.ble_devices)} BLE")

    async def start(self):
        """Initialize the Bumble device with both protocols."""
        log.info(f"HID Host v{get_version()}")

        def configure(device):
            device.classic_enabled = bool(self.classic_devices)
            device.le_enabled = bool(self.ble_devices)
            device.keystore = self.keystore
            device.pairing_config_factory = lambda conn: create_pairing_config()
            if self.classic_devices:
                device.classic_ssp_enabled = True
                device.classic_sc_enabled = True

        self.transport, self.device = await create_bumble_device(
            self.transport_spec, configure=configure)

        if self.device.address_resolution_offload:
            await self._set_device_privacy_modes()
            log.info("Controller address resolution enabled")

        # Classic-specific setup
        if self.classic_devices:
            class_of_device = 0x000104  # Computer/Desktop
            await self.device.host.send_command(
                HCI_Write_Class_Of_Device_Command(class_of_device=class_of_device),
                check_result=True
            )
            log.info(f"Classic enabled: CoD 0x{class_of_device:06X}")

            local_name_bytes = config.device_name.encode('utf-8') + b'\x00'
            await self.device.host.send_command(
                HCI_Write_Local_Name_Command(local_name=local_name_bytes),
                check_result=True
            )

        if self.ble_devices:
            log.info("BLE enabled")

        # Load keystore addresses
        await self._load_keystore_addresses()


    async def _set_device_privacy_modes(self):
        """Keep bonded peers visible when they advertise with their
        identity address instead of an RPA."""
        if not self.device.host.supports_command(HCI_LE_SET_PRIVACY_MODE_COMMAND):
            return
        for _, address in await self.keystore.get_resolving_keys():
            try:
                await self.device.send_command(
                    HCI_LE_Set_Privacy_Mode_Command(
                        peer_identity_address_type=address.address_type,
                        peer_identity_address=address,
                        privacy_mode=HCI_LE_Set_Privacy_Mode_Command.PrivacyMode.DEVICE_PRIVACY_MODE,
                    ), check_result=True)
            except Exception as e:
                log.warning(f"Privacy mode for {address}: {e}")

    async def _load_keystore_addresses(self):
        """Load addresses from keystore for connection filtering."""
        self._keystore_addresses = set()
        self._keystore_address_types = {}
        if self.keystore:
            try:
                keys = await self.keystore.get_all()
                if keys:
                    for entry in keys:
                        addr = str(entry[0]) if isinstance(entry, (list, tuple)) else str(entry)
                        self._keystore_addresses.add(normalize_addr(addr))
                        pairing_keys = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else None
                        if pairing_keys is not None and pairing_keys.address_type is not None:
                            self._keystore_address_types[normalize_addr(addr)] = pairing_keys.address_type
                    log.info(f"Keystore has {len(self._keystore_addresses)} entries")
            except Exception as e:
                log.warning(f"Failed to load keystore: {e}")

    def _format_device(self, addr: str) -> str:
        """Format device address with name if available."""
        norm = normalize_addr(addr)
        for dev in self.classic_devices + self.ble_devices:
            if dev.address == norm:
                if dev.name:
                    return f"{dev.name} ({addr})"
        return addr

    async def run(self):
        """Main run loop - handle both protocols concurrently."""
        self._disconnection_event = asyncio.Event()
        self._connection_future = asyncio.get_event_loop().create_future()

        self._parse_devices()
        await self.start()

        for dev in self.classic_devices + self.ble_devices:
            if dev.address != '*':
                cache = self.device_cache.load(dev.address)
                if cache and 'report_map' in cache:
                    log.info(f"Cached descriptor for {self._format_device(dev.address)}")

        tasks = []

        if self.classic_devices:
            tasks.append(asyncio.create_task(
                self._run_classic_handler(),
                name="classic_handler"
            ))

        if self.ble_devices:
            tasks.append(asyncio.create_task(
                self._run_ble_handler(),
                name="ble_handler"
            ))

        if not tasks:
            log.error("No devices configured")
            return

        log.info(f"Waiting for connection (Classic: {len(self.classic_devices)}, BLE: {len(self.ble_devices)})")

        try:
            await asyncio.wait_for(self._connection_future, timeout=60.0)
        except asyncio.TimeoutError:
            log.warning("Connection timeout - no device connected")
            raise InvalidStateError("No device connected within timeout")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        if self.connected_protocol == Protocol.CLASSIC:
            await self._handle_classic_connection()
        else:
            await self._handle_ble_connection()

        proto_name = self.connected_protocol.value.upper()
        log.success(f"\n[{proto_name}] Receiving HID reports. Press Ctrl+C to exit.")

        # Daemon owns the reconnect + stale-key-clear policy: on auth
        # failure it reads get_auth_failure_address(), removes the key,
        # and restarts us on a fresh transport. Only a Classic auth
        # failure while BLE is connected is absorbed here.
        while True:
            await self._disconnection_event.wait()

            if self._auth_failure_address and self.connected_protocol == Protocol.BLE and self.connection:
                log.info("[Classic] Auth failure ignored - BLE connection is active")
                self._auth_failure_address = None
                self._disconnection_event.clear()
                continue
            break

    # ==================== PAIRING ====================

    async def pair_device(self, address: str, protocol: Protocol = None) -> bool:
        """Pair with a device (first-time setup)."""
        if protocol is None:
            protocol = Protocol.BLE

        self._parse_devices()

        if protocol == Protocol.CLASSIC:
            self.classic_devices = [DeviceConfig(address=address, protocol=protocol)]
            self.ble_devices = []
        else:
            self.ble_devices = [DeviceConfig(address=address, protocol=protocol)]
            self.classic_devices = []

        await self.start()

        if protocol == Protocol.CLASSIC:
            return await self._pair_classic(address)
        else:
            return await self._pair_ble(address)

    async def _pair_ble(self, address: str) -> bool:
        """Pair with a BLE device."""
        log.info(f"[BLE] Pairing with {address}...")

        target = Address(address)
        try:
            self.connection = await self.device.connect(
                target,
                own_address_type=OwnAddressType.PUBLIC,
                timeout=config.connect_timeout,
            )
        except Exception as e:
            log.error(f"[BLE] Connection failed: {e}")
            return False

        self.peer = Peer(self.connection)
        self.current_device_address = address
        self.connected_protocol = Protocol.BLE
        log.success(f"[BLE] Connected to {address}")

        try:
            log.info("[BLE] Initiating pairing...")
            await self.connection.pair()
            log.success("[BLE] Pairing complete!")

            await self._discover_ble_hid_service()

            return True
        except Exception as e:
            log.error(f"[BLE] Pairing failed: {e}")
            if self.connection:
                try:
                    await self.connection.disconnect()
                except Exception:
                    pass
                self.connection = None
                self.peer = None
            return False

    async def _discover_ble_hid_service(self, process_reports: bool = False):
        """Discover BLE GATT HID service and cache descriptor."""
        await self.peer.discover_services()

        if not self.device_name:
            await self._read_ble_device_name()

        hid_services = [s for s in self.peer.services if s.uuid == GATT_HUMAN_INTERFACE_DEVICE_SERVICE]
        if not hid_services:
            if process_reports:
                raise InvalidStateError("[BLE] HID service not found")
            log.warning("[BLE] HID service not found")
            return

        hid_service = hid_services[0]
        log.success("[BLE] Found HID service")

        await self.peer.discover_characteristics(service=hid_service)

        for char in hid_service.characteristics:
            if char.uuid == GATT_REPORT_MAP_CHARACTERISTIC and not self.report_map:
                try:
                    value = await self.peer.read_value(char)
                    self.report_map = bytes(value)
                    log.success(f"[BLE] Got descriptor: {len(self.report_map)} bytes")

                    address = self.current_device_address
                    self.device_cache.save(address, {
                        'report_map': self.report_map.hex(),
                        'device_name': self.device_name
                    })
                except Exception as e:
                    log.warning(f"[BLE] Failed to read report map: {e}")

            elif process_reports and char.uuid == GATT_REPORT_CHARACTERISTIC:
                await self._process_ble_report_char(char)

    async def _pair_classic(self, address: str) -> bool:
        """Pair with a Classic Bluetooth device."""
        log.info(f"[Classic] Pairing with {address}...")

        try:
            target_address = Address(address, Address.PUBLIC_DEVICE_ADDRESS)
            self.connection = await self.device.connect(
                target_address,
                transport=BT_BR_EDR_TRANSPORT,
                timeout=config.connect_timeout,
            )
            log.success(f"[Classic] Connected to {address}")
        except (asyncio.TimeoutError, BumbleTimeoutError):
            log.error(f"[Classic] Connection timeout after {config.connect_timeout}s")
            return False
        except Exception as e:
            log.error(f"[Classic] Connection failed: {e}")
            return False

        self.current_device_address = address
        self.connected_protocol = Protocol.CLASSIC

        link_key_received = asyncio.Event()

        def on_device_link_key(_bd_addr, link_key, key_type):
            log.success(f"[Classic] Link key received: type={key_type}")
            link_key_received.set()

        self.device.host.on('link_key', on_device_link_key)

        try:
            log.info("[Classic] Authenticating...")
            try:
                await asyncio.wait_for(self.connection.authenticate(), timeout=30.0)
                log.success("[Classic] Authentication complete")
            except Exception as e:
                log.warning(f"[Classic] Authentication: {e}")

            log.info("[Classic] Waiting for link key...")
            try:
                await asyncio.wait_for(link_key_received.wait(), timeout=5.0)
                log.success("[Classic] Link key saved")
            except asyncio.TimeoutError:
                log.warning("[Classic] Link key event timeout (may already be saved)")

            if not self.connection.is_encrypted:
                log.info("[Classic] Requesting encryption...")
                try:
                    await asyncio.wait_for(
                        self.connection.encrypt(enable=True),
                        timeout=10.0
                    )
                except Exception as e:
                    log.warning(f"[Classic] Encryption: {e}")

            await self._query_classic_sdp(address)

            if self.keystore:
                keys = await self.keystore.get(address)
                if keys and keys.link_key:
                    log.success("[Classic] Link key verified")
                else:
                    log.warning("[Classic] Link key not found in keystore!")

            self.device.host.remove_listener('link_key', on_device_link_key)
            return True

        except Exception as e:
            log.error(f"[Classic] Pairing failed: {e}")
            self.device.host.remove_listener('link_key', on_device_link_key)
            if self.connection:
                try:
                    await self.connection.disconnect()
                except Exception:
                    pass
                self.connection = None
            return False

    async def _query_classic_sdp(self, address: str = None):
        """Query SDP for HID descriptor and cache it."""
        if not self.connection:
            return

        address = address or self.current_device_address

        log.info("[Classic] Querying SDP...")
        try:
            sdp_client = SDPClient(self.connection)
            await asyncio.wait_for(sdp_client.connect(), timeout=5.0)

            result = await asyncio.wait_for(
                sdp_client.search_attributes(
                    [BT_HUMAN_INTERFACE_DEVICE_SERVICE],
                    [0x0100, 0x0206]
                ),
                timeout=10.0
            )

            if result:
                for record in result:
                    for attr in record:
                        if hasattr(attr, 'id') and attr.id == 0x0206:
                            self._parse_hid_descriptor_list(attr.value)
                        elif hasattr(attr, 'id') and attr.id == 0x0100:
                            try:
                                name = attr.value.value
                                if isinstance(name, bytes):
                                    name = name.decode('utf-8', errors='replace')
                                self.device_name = str(name)
                            except Exception:
                                pass

            await sdp_client.disconnect()

            if self.report_map:
                self.device_cache.save(address, {
                    'report_map': self.report_map.hex(),
                    'device_name': self.device_name or 'Unknown'
                })
                log.success(f"[Classic] Cached descriptor ({len(self.report_map)} bytes)")
        except Exception as e:
            log.warning(f"[Classic] SDP query failed: {e}")

    async def continue_after_pairing(self):
        """Continue into run mode after successful pairing."""
        if not self.connected_protocol:
            raise InvalidStateError("No paired device - call pair_device first")

        if self.connected_protocol == Protocol.CLASSIC and not self.connection:
            raise InvalidStateError("No connection - call pair_device first")

        self._disconnection_event = asyncio.Event()

        if self.connection:
            self.connection.on('disconnection', self._on_disconnection)

        if self.connected_protocol == Protocol.CLASSIC:
            await self._continue_classic_after_pairing()
        else:
            await self._continue_ble_after_pairing()

        proto_name = self.connected_protocol.value.upper()
        log.success(f"\n[{proto_name}] Paired and receiving HID reports. Press Ctrl+C to exit.")

        await self._disconnection_event.wait()

    async def _continue_classic_after_pairing(self):
        """Continue Classic connection after pairing."""
        self.hid_host = BumbleHIDHost(self.device)
        self.hid_host.on(BumbleHIDHost.EVENT_INTERRUPT_DATA, self._on_classic_interrupt_data)
        self.hid_host.on(BumbleHIDHost.EVENT_VIRTUAL_CABLE_UNPLUG, self._on_virtual_cable_unplug)
        log.info("[Classic] HID Host created")

        self.hid_host.on_device_connection(self.connection)

        log.info("[Classic] Connecting to HID control channel...")
        try:
            await asyncio.wait_for(self.hid_host.connect_control_channel(), timeout=5.0)
            log.success("[Classic] HID control channel connected")
        except Exception as e:
            log.warning(f"[Classic] Control channel: {e}")

        log.info("[Classic] Connecting to HID interrupt channel...")
        try:
            await asyncio.wait_for(self.hid_host.connect_interrupt_channel(), timeout=5.0)
            log.success("[Classic] HID interrupt channel connected")
        except Exception as e:
            log.warning(f"[Classic] Interrupt channel: {e}")

        if not self.hid_host.l2cap_intr_channel:
            log.error("[Classic] Failed to connect HID interrupt channel")
            return

        self._finalize_classic_hid()

    async def _continue_ble_after_pairing(self):
        """Continue BLE connection after pairing."""
        if not self.connection:
            log.info(f"[BLE] Reconnecting to {self.current_device_address}...")
            target = Address(self.current_device_address)
            self.connection = await self.device.connect(
                target,
                own_address_type=OwnAddressType.PUBLIC,
                timeout=config.connect_timeout,
            )
            self.peer = Peer(self.connection)
            self.connection.on('disconnection', self._on_disconnection)
            await self._ble_restore_or_pair()
        else:
            log.info("[BLE] Using existing connection from pairing")
            if not self.peer:
                self.peer = Peer(self.connection)
            log.info("[BLE] Connection already encrypted")

        await self._setup_ble_hid()

    # ==================== COMMON ====================

    def _on_disconnection(self, reason):
        """Handle device disconnection."""
        proto = self.connected_protocol.value.upper() if self.connected_protocol else "Unknown"
        addr = self.current_device_address or "unknown"
        log.warning(f"[{proto}] Device disconnected: {addr} (reason={reason})")

        if reason == 5 and self.current_device_address and proto == "CLASSIC":
            log.info("[Classic] Authentication failure - will clear stale key and retry")
            self._auth_failure_address = self.current_device_address

        self._disconnection_event.set()

    def _forward_report(self, data: bytes):
        """Deduplicate, log, and forward an HID report to UHID."""
        if data != self._last_report:
            log.debug(f"Report: {data.hex()}")
            self._last_report = data
        if self.uhid_device:
            try:
                self.uhid_device.send_input(data)
            except Exception as e:
                log.warning(f"UHID send failed: {e}")

    def _load_cached_descriptor(self, address: str = None) -> bool:
        """Load report descriptor and device name from cache. Returns True if found."""
        address = address or self.current_device_address
        cache = self.device_cache.load(address)
        if cache and 'report_map' in cache:
            self.report_map = bytes.fromhex(cache['report_map'])
            self.device_name = cache.get('device_name')
            log.success(f"Loaded cached descriptor ({len(self.report_map)} bytes)")
            return True
        return False

    def _create_uhid_device(self):
        """Create UHID virtual device."""
        if not self.report_map:
            log.warning("No report descriptor for UHID")
            return

        try:
            name = self.device_name or "HID Device"
            descriptor = strip_digitizer_collections(self.report_map)
            self.uhid_device = UHIDDevice(
                name=name,
                report_descriptor=descriptor,
                bus=Bus.BLUETOOTH,
                vendor=0,
                product=0,
                uniq=self.current_device_address or "",
            )
            log.success(f"UHID device created: {name}")
            asyncio.get_event_loop().call_later(
                0.5, self.uhid_device.discover_input_paths)
        except Exception as e:
            log.error(f"Failed to create UHID device: {e}")

    def _is_connection_alive(self) -> bool:
        """Check if the connection is still alive and usable."""
        if self.connection is None:
            return False
        if not hasattr(self.connection, 'handle') or self.connection.handle is None:
            return False
        if hasattr(self.connection, 'is_disconnected') and self.connection.is_disconnected:
            return False
        return True

    async def cleanup(self):
        """Clean up resources."""
        if self._connection_tasks:
            pending = list(self._connection_tasks)
            for task in pending:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                pass
            self._connection_tasks.clear()

        if self.uhid_device:
            try:
                self.uhid_device.destroy()
            except Exception:
                pass
            self.uhid_device = None

        if self.hid_host:
            if self._is_connection_alive():
                if self.hid_host.l2cap_intr_channel:
                    try:
                        await asyncio.wait_for(
                            self.hid_host.disconnect_interrupt_channel(), timeout=1.0)
                    except (asyncio.TimeoutError, Exception):
                        pass
                if self.hid_host.l2cap_ctrl_channel:
                    try:
                        await asyncio.wait_for(
                            self.hid_host.disconnect_control_channel(), timeout=1.0)
                    except (asyncio.TimeoutError, Exception):
                        pass
            self.hid_host = None

        peer_already_disconnected = (
            self._disconnection_event is not None
            and self._disconnection_event.is_set()
        )
        if self._is_connection_alive() and not peer_already_disconnected:
            try:
                await asyncio.wait_for(self.connection.disconnect(), timeout=2.0)
            except asyncio.TimeoutError:
                log.warning("Connection disconnect timed out")
            except Exception as e:
                log.debug(f"Disconnect cleanup: {e}")
        self.connection = None
        self.peer = None

        if hasattr(self, '_classic_connection_listener') and self._classic_connection_listener:
            try:
                self.device.remove_listener('connection', self._classic_connection_listener)
            except Exception:
                pass
            self._classic_connection_listener = None

        if self.transport:
            try:
                await asyncio.wait_for(self.transport.close(), timeout=3.0)
            except asyncio.TimeoutError:
                log.warning("Transport close timed out, fd may leak")
            except Exception:
                pass
            self.transport = None

    def get_auth_failure_address(self) -> str:
        """Get address that had auth failure, if any."""
        addr = self._auth_failure_address
        self._auth_failure_address = None
        return addr
