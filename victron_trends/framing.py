"""Message framing on Victron's vendor GATT service: VE.Direct registers in CBOR.

    client -> device   05 <sub> [reg, ...]       GET
    client -> device   06 <sub> [reg, value]     SET
    device -> client   08 <sub> reg value        VALUE
    device -> client   09 <sub> reg status       STATUS

Register ids are CBOR unsigned integers. Values are CBOR byte strings whose
contents are little-endian. Only the CBOR subset these messages use is
implemented: unsigned and negative integers, byte strings and arrays.
"""
from __future__ import annotations

from typing import NamedTuple

# Second byte of every Trends request. Its meaning is unknown; the app always sends 03.
SUB = 0x03

GET, SET, VALUE, STATUS = 0x05, 0x06, 0x08, 0x09
KIND_NAMES = {GET: "GET", SET: "SET", VALUE: "VALUE", STATUS: "STATUS"}

# The longest reply seen is 71 bytes (a 64-byte block plus framing). A partial
# message at the end of a longer buffer can't be one that's still arriving.
MAX_REPLY = 100


class Incomplete(Exception):
    """The buffer ends partway through a CBOR item."""


class Message(NamedTuple):
    kind: str  # "GET", "SET", "VALUE" or "STATUS"
    sub: int
    reg: int | list[int]  # a list for GET, which can ask for several registers
    value: bytes | int | None


def decode_item(buf: bytes, pos: int):
    """Decode one CBOR item at buf[pos]. Returns (item, position after it)."""
    if pos >= len(buf):
        raise Incomplete
    major, info = buf[pos] >> 5, buf[pos] & 0x1F
    pos += 1
    if info < 24:
        arg = info
    elif info <= 27:
        size = 1 << (info - 24)
        if pos + size > len(buf):
            raise Incomplete
        arg = int.from_bytes(buf[pos:pos + size], "big")
        pos += size
    else:
        raise ValueError(f"unsupported CBOR length encoding {info}")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major == 2:
        if pos + arg > len(buf):
            raise Incomplete
        return bytes(buf[pos:pos + arg]), pos + arg
    if major == 4:
        items = []
        for _ in range(arg):
            item, pos = decode_item(buf, pos)
            items.append(item)
        return items, pos
    raise ValueError(f"unsupported CBOR major type {major}")


def encode_uint(n: int) -> bytes:
    if n < 24:
        return bytes([n])
    if n < 0x100:
        return bytes([0x18, n])
    if n < 0x10000:
        return bytes([0x19]) + n.to_bytes(2, "big")
    raise ValueError(f"register id {n:#x} out of range")


def encode_bytes(b: bytes) -> bytes:
    if len(b) < 24:
        return bytes([0x40 | len(b)]) + b
    if len(b) < 0x100:
        return bytes([0x58, len(b)]) + b
    raise ValueError("value too long")


def encode_get(reg: int, sub: int = SUB) -> bytes:
    return bytes([GET, sub, 0x81]) + encode_uint(reg)


def encode_set(reg: int, payload: bytes, sub: int = SUB) -> bytes:
    return bytes([SET, sub, 0x82]) + encode_uint(reg) + encode_bytes(payload)


# SET 0x93 = 10000, sent with sub 00. The device drops the link about 10 s after
# the last one; VictronConnect re-sends it at least every 3.5 s.
KEEPALIVE = encode_set(0x93, (10_000).to_bytes(2, "little"), sub=0x00)


def _decode_at(buf: bytes, pos: int) -> tuple[Message, int]:
    """Decode the message starting at buf[pos], or raise ValueError/Incomplete."""
    kind, sub = buf[pos], buf[pos + 1]
    if kind in (VALUE, STATUS):
        reg, pos = decode_item(buf, pos + 2)
        value, pos = decode_item(buf, pos)
        if not isinstance(reg, int):
            raise ValueError("register id is not an integer")
        return Message(KIND_NAMES[kind], sub, reg, value), pos
    if kind == GET:
        regs, pos = decode_item(buf, pos + 2)
        if not isinstance(regs, list):
            raise ValueError("GET without a register list")
        return Message("GET", sub, regs, None), pos
    if kind == SET:
        pair, pos = decode_item(buf, pos + 2)
        if not (isinstance(pair, list) and len(pair) == 2):
            raise ValueError("SET without a [register, value] pair")
        return Message("SET", sub, pair[0], pair[1]), pos
    raise ValueError(f"unknown message kind {kind:#04x}")


def parse_messages(buf: bytes) -> tuple[list[Message], int]:
    """Every message in a complete byte stream, such as one direction of a capture.

    Bytes that don't start a valid message are skipped one at a time, so the parser
    resynchronizes after anything it doesn't understand. Returns (messages, bytes skipped).
    """
    messages, skipped, pos = [], 0, 0
    while pos < len(buf) - 1:
        try:
            message, pos = _decode_at(buf, pos)
        except (Incomplete, ValueError):
            pos += 1
            skipped += 1
            continue
        messages.append(message)
    return messages, skipped


class ReplyStream:
    """Incremental parser for device replies arriving in notification-sized pieces."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[Message]:
        self.buf.extend(data)
        out, pos = [], 0
        while pos < len(self.buf) - 1:
            if self.buf[pos] not in (VALUE, STATUS):
                pos += 1
                continue
            try:
                message, end = _decode_at(self.buf, pos)
            except Incomplete:
                if len(self.buf) - pos < MAX_REPLY:
                    break  # the rest of this message is still on its way
                pos += 1
                continue
            except ValueError:
                pos += 1
                continue
            out.append(message)
            pos = end
        del self.buf[:pos]
        return out
