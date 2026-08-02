#!/usr/bin/env python3
"""Tests for the resyncing HCI parser (issue #120). Run: python3 tests/test_hci_parser.py"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'kindle_hid_passthrough'))

from hci_parser import ResyncingPacketParser  # noqa: E402


class Sink:
    def __init__(self):
        self.packets = []

    def on_packet(self, packet):
        self.packets.append(packet)


def parser():
    p = ResyncingPacketParser()
    sink = Sink()
    p.set_packet_sink(sink)
    return p, sink


def event(code=0x0E, body=b'\x01\x03\x0c\x00'):
    return bytes([0x04, code, len(body)]) + body


def acl(handle=0x0040, body=b'\xaa' * 8):
    return bytes([0x02]) + handle.to_bytes(2, 'little') \
        + len(body).to_bytes(2, 'little') + body


def test_clean_stream():
    p, sink = parser()
    p.feed_data(event() + acl() + event())
    assert len(sink.packets) == 3, sink.packets
    assert sink.packets[0] == event()
    assert sink.packets[1] == acl()


def test_split_across_reads():
    p, sink = parser()
    data = event() + acl()
    for i in range(len(data)):
        p.feed_data(data[i:i + 1])
    assert len(sink.packets) == 2, sink.packets


def test_resync_after_garbage():
    """Junk between packets is discarded, the next good packet still lands."""
    p, sink = parser()
    p.feed_data(event() + b'\x14\x00\x00\x00' + event(code=0x0F))
    assert len(sink.packets) == 2, sink.packets
    assert sink.packets[1][1] == 0x0F


def test_command_type_is_not_accepted_from_controller():
    """0x01 is host-to-controller; seeing it means we are desynced."""
    p, sink = parser()
    p.feed_data(b'\x01\x03\x0c\x00' + event())
    assert len(sink.packets) == 1, sink.packets
    assert sink.packets[0] == event()


def test_implausible_acl_length_does_not_swallow_the_stream():
    """The bug behind issue #120: a bogus length ate the next 8 KB."""
    p, sink = parser()
    bogus = bytes([0x02, 0x40, 0x00, 0x40, 0x20])  # claims 8256 bytes
    p.feed_data(bogus + event())
    assert len(sink.packets) == 1, sink.packets
    assert sink.packets[0] == event()


def test_dropped_leading_bytes_recover_on_next_packet():
    """Bytes lost to a deep-idle wake cost one packet, not the session."""
    p, sink = parser()
    truncated = event(body=b'\x01\x03\x0c\x00')[5:]  # lost the first 5 bytes
    p.feed_data(truncated)
    for _ in range(4):
        p.feed_data(event())
    assert len(sink.packets) >= 3, sink.packets
    assert sink.packets[-1] == event()


def test_stale_partial_is_flushed():
    """A half-read packet whose tail never arrives must not poison what follows."""
    p, sink = parser()
    p.feed_data(acl(body=b'\xaa' * 200)[:10])  # header promises 200, sends 9
    p._partial_since -= 10.0                   # age it past PARTIAL_TIMEOUT
    p.feed_data(event())
    assert sink.packets == [event()], sink.packets


def test_sink_exception_does_not_stall_parser():
    class Boom:
        def __init__(self):
            self.count = 0

        def on_packet(self, packet):
            self.count += 1
            raise ValueError("boom")

    p = ResyncingPacketParser()
    sink = Boom()
    p.set_packet_sink(sink)
    p.feed_data(event() + event())
    assert sink.count == 2


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
