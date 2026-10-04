"""Android Bluetooth HCI snoop logs (btsnoop) of VictronConnect sessions.

Raw captures can contain secrets: pairing keys, and the app reads 0xEC65 (very
likely the Instant Readout key) on every connect. Share register_traffic()
output filtered to the Trends registers, never the capture itself.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

from .decode import sync_pairs, u32_list
from .framing import Message, parse_messages
from .reader import (CURSORS, REG_BLOCK, REG_BOOT_HISTORY, REG_CLOCK, REG_SYNC_HISTORY, REG_TIME_SYNC, DeviceStatus,
                     check_cursor)

BTSNOOP_MAGIC = b"btsnoop\x00"
BTSNOOP_EPOCH_US = 0x00DCDDB30F2F8000  # microseconds from year 0 to 1970
ATT_CID = 0x0004
ATT_NOTIFY, ATT_INDICATE, ATT_WRITE_REQ, ATT_WRITE_CMD = 0x1B, 0x1D, 0x12, 0x52
# ATT handles of 306b0003 and 306b0004 on the devices captured so far.
REGISTER_HANDLES = (0x0024, 0x0027)


@dataclass
class Connection:
    start: float
    end: float | None = None
    att: list[tuple[float, bool, bytes]] = field(default_factory=list)  # (time, sent by phone, ATT PDU)


def records(path) -> Iterator[tuple[float, bool, bytes]]:
    """(Unix time, sent by the phone, HCI packet) for each record in a btsnoop file."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != BTSNOOP_MAGIC:
        raise ValueError(f"{path} is not a btsnoop file")
    pos = 16
    while pos + 24 <= len(data):
        _orig_len, incl_len, flags, _drops, ts = struct.unpack_from(">IIIIq", data, pos)
        pos += 24
        yield (ts - BTSNOOP_EPOCH_US) / 1e6, not flags & 0x01, data[pos:pos + incl_len]
        pos += incl_len


def connections(path, address: str) -> list[Connection]:
    """Every LE connection to `address` in a capture, with its ATT traffic.

    ACL continuation fragments are reassembled into whole L2CAP frames first.
    """
    address = address.upper()
    found, open_conns, partial = [], {}, {}
    for t, sent, pkt in records(path):
        if len(pkt) < 3:
            continue
        if pkt[0] == 0x04:  # HCI event
            event = pkt[1]
            if event == 0x3E and len(pkt) >= 15 and pkt[3] in (0x01, 0x0A):  # LE (enhanced) connection complete
                status, handle = pkt[4], struct.unpack_from("<H", pkt, 5)[0] & 0x0FFF
                peer = ":".join(f"{b:02X}" for b in reversed(pkt[9:15]))
                if status == 0 and peer == address:
                    open_conns[handle] = Connection(start=t)
                    found.append(open_conns[handle])
            elif event == 0x05 and len(pkt) >= 7:  # disconnection complete
                conn = open_conns.pop(struct.unpack_from("<H", pkt, 4)[0] & 0x0FFF, None)
                if conn:
                    conn.end = t
        elif pkt[0] == 0x02 and len(pkt) >= 5:  # ACL data
            flags = struct.unpack_from("<H", pkt, 1)[0]
            handle, continuation = flags & 0x0FFF, (flags >> 12) & 0x3 == 0x1
            if handle not in open_conns:
                continue
            key = (handle, sent)
            if continuation:
                if key not in partial:
                    continue
                partial[key] += pkt[5:]
            else:
                partial[key] = bytearray(pkt[5:])
            frame = partial[key]
            if len(frame) < 4:
                continue
            length, cid = struct.unpack_from("<HH", frame)
            if len(frame) < 4 + length:
                continue  # more fragments to come
            del partial[key]
            if cid == ATT_CID and length:
                open_conns[handle].att.append((t, sent, bytes(frame[4:4 + length])))
    return found


def register_traffic(conn: Connection) -> tuple[list[Message], list[Message]]:
    """(device replies, phone requests) on the register channel, in order.

    The two register characteristics carry one byte stream per direction, so each
    direction is concatenated across both before parsing.
    """
    from_device, from_phone = bytearray(), bytearray()
    for _t, sent, pdu in conn.att:
        if len(pdu) < 3 or struct.unpack_from("<H", pdu, 1)[0] not in REGISTER_HANDLES:
            continue
        if not sent and pdu[0] in (ATT_NOTIFY, ATT_INDICATE):
            from_device += pdu[3:]
        elif sent and pdu[0] in (ATT_WRITE_CMD, ATT_WRITE_REQ):
            from_phone += pdu[3:]
    return parse_messages(bytes(from_device))[0], parse_messages(bytes(from_phone))[0]


class ReplaySession:
    """Answers reader.pull() from a recorded VictronConnect session.

    Plain reads return what the device sent the app. Block requests return the
    block the device sent for the same request. History pages are rebuilt from
    every entry the app saw, so any cursor can be answered.
    """

    def __init__(self, replies: list[Message], requests: list[Message]):
        values = {}
        for m in replies:
            if m.kind == "VALUE":
                values.setdefault(m.reg, []).append(m.value)
        self.static = {reg: vals[0] for reg, vals in values.items()}
        block_requests = [m.value for m in requests if m.kind == "SET" and m.reg == REG_BLOCK]
        self.blocks = dict(zip(block_requests, values.get(REG_BLOCK, [])))
        self.syncs = {p for page in values.get(REG_SYNC_HISTORY, []) for p in sync_pairs(page)}
        self.boots = {b for page in values.get(REG_BOOT_HISTORY, []) for b in u32_list(page)}
        self.writes = []
        # The Unix time of the clock reading, from the time sync the app wrote.
        app_syncs = [m.value for m in requests if m.kind == "SET" and m.reg == REG_TIME_SYNC]
        self.app_time = None
        if app_syncs and REG_CLOCK in self.static:
            counter, unix = struct.unpack("<II", app_syncs[-1])
            self.app_time = unix - (counter - struct.unpack("<I", self.static[REG_CLOCK])[0])

    @classmethod
    def from_capture(cls, path, address: str, index: int = 0) -> ReplaySession:
        return cls(*register_traffic(connections(path, address)[index]))

    async def get(self, reg: int) -> bytes:
        if reg in CURSORS:
            raise DeviceStatus(reg, 3)  # what a device answers to a plain read of a cursor
        if reg not in self.static:
            raise DeviceStatus(reg, -5)
        return self.static[reg]

    async def set(self, reg: int, payload: bytes, accept=None) -> bytes:
        check_cursor(reg)
        self.writes.append(reg)
        payload = bytes(payload)
        if reg == REG_BLOCK:
            # A request the app never made: answer "no data", an empty block at that time.
            reply = self.blocks.get(payload, payload[:5] + b"\x00\x00\x00")
        else:
            cursor = struct.unpack("<I", payload)[0]
            if reg == REG_SYNC_HISTORY:
                page = sorted((p for p in self.syncs if p[0] <= cursor), reverse=True)[:8]
                reply = b"".join(struct.pack("<II", *p) for p in page)
            else:
                page = sorted((b for b in self.boots if b <= cursor), reverse=True)[:16]
                reply = b"".join(struct.pack("<I", b) for b in page)
            reply = reply.ljust(64, b"\xff")
        if accept is not None and not accept(reply):
            raise AssertionError(f"reply filter rejected the correct reply for {reg:#06x}")
        return reply
