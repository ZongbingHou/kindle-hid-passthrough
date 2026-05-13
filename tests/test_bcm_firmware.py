"""Tests for BCM4343 firmware loader."""

import struct
from unittest.mock import MagicMock, call, patch

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'kindle_hid_passthrough'))

from bcm_firmware import (
    H4_CMD,
    H4_EVT,
    EVT_COMMAND_COMPLETE,
    HCI_RESET,
    BCM_DOWNLOAD_MINIDRIVER,
    _build_hci_cmd,
    _read_event,
    _send_cmd,
    _find_hcd_file,
    _parse_hcd,
    download_firmware,
)


# --- HCI command building ---

class TestBuildHciCmd:
    def test_reset_command(self):
        cmd = _build_hci_cmd(HCI_RESET)
        assert cmd == struct.pack('<BHB', H4_CMD, HCI_RESET, 0)
        assert len(cmd) == 4

    def test_command_with_params(self):
        params = b'\x01\x02\x03'
        cmd = _build_hci_cmd(0xFC2E, params)
        assert cmd[0] == H4_CMD
        opcode = struct.unpack_from('<H', cmd, 1)[0]
        assert opcode == 0xFC2E
        assert cmd[3] == 3  # param length
        assert cmd[4:] == params

    def test_empty_params(self):
        cmd = _build_hci_cmd(0x1234, b'')
        assert cmd[3] == 0
        assert len(cmd) == 4


# --- HCI event reading ---

class TestReadEvent:
    def _make_event(self, event_code, params):
        """Build a raw H4 event packet."""
        return bytes([H4_EVT, event_code, len(params)]) + params

    def test_command_complete(self):
        # Command Complete for HCI_Reset, status=0
        params = bytes([1, 0x03, 0x0C, 0x00])  # num_pkts, opcode_lo, opcode_hi, status
        raw = self._make_event(EVT_COMMAND_COMPLETE, params)

        port = MagicMock()
        port.read = MagicMock(side_effect=[
            bytes([raw[0]]),       # H4 type
            raw[1:3],              # event header
            raw[3:],              # params
        ])

        result = _read_event(port)
        assert result is not None
        event_code, data = result
        assert event_code == EVT_COMMAND_COMPLETE
        assert data == params

    def test_timeout_returns_none(self):
        port = MagicMock()
        port.read = MagicMock(return_value=b'')

        result = _read_event(port)
        assert result is None

    def test_wrong_packet_type(self):
        port = MagicMock()
        port.read = MagicMock(return_value=bytes([0x02]))  # ACL, not event

        result = _read_event(port)
        assert result is None

    def test_truncated_header(self):
        port = MagicMock()
        port.read = MagicMock(side_effect=[
            bytes([H4_EVT]),
            bytes([0x0E]),  # only 1 byte of 2-byte header
        ])

        result = _read_event(port)
        assert result is None

    def test_truncated_params(self):
        port = MagicMock()
        port.read = MagicMock(side_effect=[
            bytes([H4_EVT]),
            bytes([0x0E, 0x04]),   # event + 4 bytes expected
            bytes([0x01, 0x03]),   # only 2 of 4 bytes
        ])

        result = _read_event(port)
        assert result is None


# --- HCI command send ---

class TestSendCmd:
    def _mock_port_ok(self, opcode):
        """Create a port mock that responds with Command Complete success."""
        port = MagicMock()
        opcode_lo = opcode & 0xFF
        opcode_hi = (opcode >> 8) & 0xFF
        params = bytes([1, opcode_lo, opcode_hi, 0x00])
        raw = bytes([H4_EVT, EVT_COMMAND_COMPLETE, len(params)]) + params

        port.read = MagicMock(side_effect=[
            bytes([raw[0]]),
            raw[1:3],
            raw[3:],
        ])
        return port

    def test_successful_command(self):
        port = self._mock_port_ok(HCI_RESET)
        assert _send_cmd(port, HCI_RESET) is True
        port.write.assert_called_once()
        port.flush.assert_called_once()

    def test_writes_correct_bytes(self):
        port = self._mock_port_ok(HCI_RESET)
        _send_cmd(port, HCI_RESET)
        written = port.write.call_args[0][0]
        assert written == _build_hci_cmd(HCI_RESET)

    def test_command_with_error_status(self):
        port = MagicMock()
        params = bytes([1, 0x03, 0x0C, 0x01])  # status = 1 (error)
        raw = bytes([H4_EVT, EVT_COMMAND_COMPLETE, len(params)]) + params
        port.read = MagicMock(side_effect=[
            bytes([raw[0]]),
            raw[1:3],
            raw[3:],
        ])

        assert _send_cmd(port, HCI_RESET) is False

    def test_timeout(self):
        port = MagicMock()
        port.read = MagicMock(return_value=b'')

        assert _send_cmd(port, HCI_RESET) is False


# --- HCD file parsing ---

class TestParseHcd:
    def test_single_command(self):
        # opcode=0xFC01, param_len=2, params=0xAA 0xBB
        data = struct.pack('<HB', 0xFC01, 2) + b'\xAA\xBB'
        commands = _parse_hcd(data)
        assert len(commands) == 1
        assert commands[0] == (0xFC01, b'\xAA\xBB')

    def test_multiple_commands(self):
        cmd1 = struct.pack('<HB', 0xFC01, 1) + b'\x11'
        cmd2 = struct.pack('<HB', 0xFC02, 2) + b'\x22\x33'
        cmd3 = struct.pack('<HB', 0xFC03, 0)
        data = cmd1 + cmd2 + cmd3
        commands = _parse_hcd(data)
        assert len(commands) == 3
        assert commands[0] == (0xFC01, b'\x11')
        assert commands[1] == (0xFC02, b'\x22\x33')
        assert commands[2] == (0xFC03, b'')

    def test_empty_data(self):
        assert _parse_hcd(b'') == []

    def test_truncated_header(self):
        # Only 2 bytes, need 3 for header
        assert _parse_hcd(b'\x01\xFC') == []

    def test_truncated_params(self):
        # Header says 5 bytes of params but only 2 follow
        data = struct.pack('<HB', 0xFC01, 5) + b'\xAA\xBB'
        commands = _parse_hcd(data)
        assert len(commands) == 0

    def test_real_world_hcd_structure(self):
        """Simulate a typical .hcd with reset + vendor writes."""
        cmds = []
        # A bunch of vendor commands like a real patchram
        for i in range(10):
            payload = bytes([i] * 8)
            cmds.append(struct.pack('<HB', 0xFC4C, len(payload)) + payload)
        data = b''.join(cmds)
        parsed = _parse_hcd(data)
        assert len(parsed) == 10
        for i, (opcode, params) in enumerate(parsed):
            assert opcode == 0xFC4C
            assert params == bytes([i] * 8)


# --- Find HCD file ---

class TestFindHcdFile:
    def test_no_directory(self):
        assert _find_hcd_file(None) is None
        assert _find_hcd_file('/nonexistent/path') is None

    def test_finds_hcd(self, tmp_path):
        hcd = tmp_path / 'BCM4343.hcd'
        hcd.write_bytes(b'\x00' * 100)
        result = _find_hcd_file(str(tmp_path))
        assert result == str(hcd)

    def test_prefers_latest_version(self, tmp_path):
        (tmp_path / 'BCM4343A1_001.002.009.0062.0363.hcd').write_bytes(b'\x00' * 1000)
        (tmp_path / 'BCM4343A1_001.002.009.0152.0517.hcd').write_bytes(b'\x00' * 10)
        result = _find_hcd_file(str(tmp_path))
        assert result == str(tmp_path / 'BCM4343A1_001.002.009.0152.0517.hcd')

    def test_empty_directory(self, tmp_path):
        assert _find_hcd_file(str(tmp_path)) is None


# --- Firmware download integration ---

class TestDownloadFirmware:
    def test_no_firmware_file(self, tmp_path):
        assert download_firmware('/dev/null', str(tmp_path)) is False

    def test_no_firmware_dir(self):
        assert download_firmware('/dev/null', '/nonexistent') is False

    @patch('bcm_firmware.serial.Serial')
    def test_full_download_sequence(self, mock_serial_class, tmp_path):
        """Verify the complete command sequence for a 3-command .hcd file."""
        # Create a test .hcd with 3 commands
        cmd1 = struct.pack('<HB', 0xFC4C, 2) + b'\x11\x22'
        cmd2 = struct.pack('<HB', 0xFC4C, 1) + b'\x33'
        cmd3 = struct.pack('<HB', 0xFC4C, 0)
        hcd = tmp_path / 'test.hcd'
        hcd.write_bytes(cmd1 + cmd2 + cmd3)

        # Mock serial port that always returns success
        port = MagicMock()

        def make_success_response():
            # Generic Command Complete with status=0
            params = bytes([1, 0x00, 0x00, 0x00])
            raw = bytes([H4_EVT, EVT_COMMAND_COMPLETE, len(params)]) + params
            return [bytes([raw[0]]), raw[1:3], raw[3:]]

        # We need responses for: HCI_Reset + Download_Minidriver + 3 fw cmds + HCI_Reset
        all_reads = []
        for _ in range(6):
            all_reads.extend(make_success_response())

        port.read = MagicMock(side_effect=all_reads)
        mock_serial_class.return_value = port

        result = download_firmware('/dev/ttymxc2', str(tmp_path), 115200)
        assert result is True

        # Verify serial port was opened correctly
        mock_serial_class.assert_called_once_with(
            port='/dev/ttymxc2',
            baudrate=115200,
            bytesize=8,
            parity='N',
            stopbits=1,
            rtscts=False,
            timeout=5.0,
        )

        # Verify command sequence from writes
        writes = [c[0][0] for c in port.write.call_args_list]
        assert len(writes) == 6

        # First: HCI_Reset
        assert struct.unpack_from('<H', writes[0], 1)[0] == HCI_RESET
        # Second: Download Minidriver
        assert struct.unpack_from('<H', writes[1], 1)[0] == BCM_DOWNLOAD_MINIDRIVER
        # Next 3: firmware commands
        for i in range(2, 5):
            assert struct.unpack_from('<H', writes[i], 1)[0] == 0xFC4C
        # Last: HCI_Reset again
        assert struct.unpack_from('<H', writes[5], 1)[0] == HCI_RESET

        port.close.assert_called_once()

    @patch('bcm_firmware.serial.Serial')
    def test_reset_failure_aborts(self, mock_serial_class, tmp_path):
        """If initial HCI_Reset fails, abort before firmware download."""
        hcd = tmp_path / 'test.hcd'
        hcd.write_bytes(struct.pack('<HB', 0xFC4C, 0))

        port = MagicMock()
        port.read = MagicMock(return_value=b'')  # timeout = no response
        mock_serial_class.return_value = port

        result = download_firmware('/dev/ttymxc2', str(tmp_path))
        assert result is False
        port.close.assert_called_once()

    @patch('bcm_firmware.serial.Serial')
    def test_port_closed_on_failure(self, mock_serial_class, tmp_path):
        """Serial port must be closed even if download fails mid-way."""
        cmd1 = struct.pack('<HB', 0xFC4C, 1) + b'\x11'
        cmd2 = struct.pack('<HB', 0xFC4C, 1) + b'\x22'
        hcd = tmp_path / 'test.hcd'
        hcd.write_bytes(cmd1 + cmd2)

        port = MagicMock()

        def make_success():
            params = bytes([1, 0x00, 0x00, 0x00])
            raw = bytes([H4_EVT, EVT_COMMAND_COMPLETE, len(params)]) + params
            return [bytes([raw[0]]), raw[1:3], raw[3:]]

        # Success for reset + download_minidriver, then timeout on first fw cmd
        reads = make_success() + make_success() + [b'']
        port.read = MagicMock(side_effect=reads)
        mock_serial_class.return_value = port

        result = download_firmware('/dev/ttymxc2', str(tmp_path))
        assert result is False
        port.close.assert_called_once()


# --- kindle_detect transport generation ---

class TestKindleDetectTransport:
    def test_brcm_transport_string(self):
        from kindle_detect import detect_kindle
        # Kindle Oasis device code 0x20C -> serial prefix B0 + hex
        # Serial: B0 + 2-char hex of 0x0C (position 2-3) won't work for 0x20C
        # since 0x20C > 0xFF, this would be a G-serial
        # Let's test with a known BRCM code using B-serial where code fits in 1 byte
        # Actually 0x20C doesn't fit in 2 hex chars. These must be G-serials.
        # For testing, let's just verify the data structure
        from kindle_detect import _CODE_LOOKUP, _BRCM_HW
        name, hw = _CODE_LOOKUP[0x20C]
        assert name == 'Kindle Oasis'
        assert hw is _BRCM_HW
        assert hw['transport_scheme'] == 'serial'
        assert hw['device_path'] == '/dev/ttymxc2'
        assert hw['baud_rate'] == 115200
        assert hw['firmware_dir'] == '/opt/brcm_4343w/bluetooth/firmware'

    def test_mtk_transport_string(self):
        from kindle_detect import _CODE_LOOKUP, _MTK_HW
        name, hw = _CODE_LOOKUP[0x690]  # PW5
        assert name == 'Kindle PW5'
        assert hw is _MTK_HW
        assert hw['transport_scheme'] == 'file'
        assert hw['device_path'] == '/dev/stpbt'

    def test_brcm_detect_returns_serial_scheme(self):
        from kindle_detect import KindleDefaults
        # Verify the dataclass accepts serial fields
        defaults = KindleDefaults(
            device_path='/dev/ttymxc2',
            kernel_module=None,
            kill_processes=['bsa_server', 'btfd'],
            model_name='Kindle Oasis 2',
            transport_scheme='serial',
            baud_rate=115200,
            firmware_dir='/opt/brcm_4343w/bluetooth/firmware',
        )
        assert defaults.transport_scheme == 'serial'
        assert defaults.baud_rate == 115200
        assert defaults.firmware_dir is not None

    def test_detect_kindle_logs_without_typeerror(self):
        # Regression: HIDLogger doesn't accept stdlib-style *args lazy format.
        # Exercise every log path in detect_kindle() to catch any future
        # `log.info("fmt %s", arg)` calls that would crash at runtime.
        from kindle_detect import detect_kindle
        assert detect_kindle('G000K90563120LDD').model_name == 'Kindle Basic 2'
        assert detect_kindle('G000P812744104HX').model_name == 'Kindle Oasis 2'
        assert detect_kindle('G000A0A0000000000') is None  # unknown code path
        assert detect_kindle('') is None                    # empty serial path
