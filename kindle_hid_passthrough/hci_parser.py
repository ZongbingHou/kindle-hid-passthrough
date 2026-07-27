#!/usr/bin/env python3
"""Resynchronizing HCI packet parser for UART transports (issue #120).

Bumble's stream parser trusts the length field: a corrupted header claiming
8 KB silently swallows the next 8 KB of good HCI, which on a BLE HID link is
minutes of dead input. And an unknown packet type raises out of feed_data,
discarding the rest of that read. On a 2 Mbaud UART that drops bytes when the
SoC wakes from deep idle, one lost byte costs the whole session.

This parser only accepts controller-to-host packet types, bounds every length
field, and resyncs a byte at a time instead of trusting a bad header.
"""

import time

from logging_utils import errstr, log

__all__ = ['ResyncingPacketParser', 'install_resync_parser']

# type -> (header_len, length_offset, length_size, length_mask, max_body)
# offsets are relative to the first byte after the packet type indicator.
# Only ACL and Event: this is a HID host, it never sets up SCO or ISO, so
# accepting those types would only give noise more ways to look like a packet.
RX_PACKET_INFO = {
    0x02: (4, 2, 2, 0xFFFF, 1024),  # ACL data
    0x04: (2, 1, 1, 0x00FF, 255),   # Event
}

PARTIAL_TIMEOUT = 2.0   # a half-read packet this stale means bytes were lost
REPORT_INTERVAL = 10.0  # rate limit for resync logging


class ResyncingPacketParser:
    """Drop-in replacement for bumble's PacketParser on the RX side."""

    def __init__(self):
        self.sink = None
        self.buffer = bytearray()
        self._partial_since = None
        self._dropped = 0
        self._last_report = 0.0

    def set_packet_sink(self, sink):
        self.sink = sink

    def reset(self):
        self.buffer.clear()
        self._partial_since = None

    def _drop_byte(self):
        del self.buffer[0]
        self._dropped += 1

    def _report(self, now):
        if not self._dropped or now - self._last_report < REPORT_INTERVAL:
            return
        log.warning(f"HCI resync: discarded {self._dropped} corrupt byte(s)")
        self._dropped = 0
        self._last_report = now

    def feed_data(self, data):
        now = time.monotonic()

        # A packet left half-read this long means the rest was lost on the wire;
        # keeping it only corrupts whatever arrives next.
        if (self._partial_since is not None
                and now - self._partial_since > PARTIAL_TIMEOUT):
            self._dropped += len(self.buffer)
            self.buffer.clear()
            self._partial_since = None

        self.buffer.extend(data)

        while self.buffer:
            info = RX_PACKET_INFO.get(self.buffer[0])
            if info is None:
                self._drop_byte()
                continue

            header_len, offset, size, mask, max_body = info
            if len(self.buffer) < 1 + header_len:
                break

            start = 1 + offset
            body = int.from_bytes(
                self.buffer[start:start + size], 'little') & mask
            if body > max_body:
                self._drop_byte()
                continue

            total = 1 + header_len + body
            if len(self.buffer) < total:
                break

            packet = bytes(self.buffer[:total])
            del self.buffer[:total]
            self._partial_since = None
            if self.sink:
                try:
                    self.sink.on_packet(packet)
                except Exception as e:
                    log.error(f"Exception in HCI packet sink: {errstr(e)}")

        if self.buffer and self._partial_since is None:
            self._partial_since = now
        self._report(now)


def install_resync_parser(transport):
    """Swap the resyncing parser in before bumble attaches the packet sink."""
    source = getattr(transport, 'source', None)
    if source is None or not hasattr(source, 'parser'):
        log.warning("Transport has no parser to replace; resync disabled")
        return False
    parser = ResyncingPacketParser()
    # Normally the sink is attached later, by Device.with_hci; carry it over if
    # that ever changes, so we can't silently swallow the whole stream.
    parser.set_packet_sink(getattr(source.parser, 'sink', None))
    source.parser = parser
    return True
