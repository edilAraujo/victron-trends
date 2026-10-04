"""bleak transport for reader.pull()."""
from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient
from bleak.exc import BleakError

from .framing import KEEPALIVE, ReplyStream, encode_get, encode_set
from .reader import DeviceStatus, check_cursor, pull

log = logging.getLogger(__name__)

UUID_CONTROL = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
UUID_REGISTERS = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
UUID_REGISTERS_2 = "306b0004-b081-4037-83dc-e59fcc3cdfd0"

# Session setup, verbatim from VictronConnect. fa80ff and f980 go to the control
# characteristic: the first negotiates message sizes (patlux/ve-smart-telemetry
# names it writeCborChunkSize), the second lets the device send 128 notifications.
# What 01 and 0300 do is unknown. The last write is the keep-alive (framing.KEEPALIVE)
# plus 4 bytes whose meaning is also unknown.
HANDSHAKE = [
    (UUID_CONTROL, "fa80ff"),
    (UUID_CONTROL, "f980"),
    (UUID_REGISTERS, "01"),
    (UUID_REGISTERS, "0300"),
    (UUID_REGISTERS, "060082189342102703010303"),
]

KEEPALIVE_INTERVAL = 2.0  # the device drops the link ~10 s after the last keep-alive

# Flow control: each "f9 41" lets the device send 65 more notifications. Without
# it the device goes silent after 128 and keeps the link open.
CREDIT_GRANT = bytes.fromhex("f941")
CREDIT_STEP = 0x41

REPLY_TIMEOUT = 3.0
TRIES = 3


class BleSession:
    """Register session over an already-connected bleak client."""

    def __init__(self, client: BleakClient):
        self.client = client
        self.stream = ReplyStream()
        self.replies: asyncio.Queue = asyncio.Queue()
        self.notifications = 0
        self.granted = 0
        self.last_keepalive = 0.0
        self.resends = 0

    def _on_notify(self, _char, data: bytearray) -> None:
        # The two register characteristics form one stream: messages span both.
        self.notifications += 1
        for message in self.stream.feed(bytes(data)):
            self.replies.put_nowait(message)

    async def start(self) -> None:
        await self.client.start_notify(UUID_CONTROL, lambda _char, _data: None)
        for uuid in (UUID_REGISTERS, UUID_REGISTERS_2):
            await self.client.start_notify(uuid, self._on_notify)
        for uuid, payload in HANDSHAKE:
            await self.client.write_gatt_char(uuid, bytes.fromhex(payload), response=False)
            await asyncio.sleep(0.15)
        await asyncio.sleep(1.0)
        while not self.replies.empty():  # values the device pushes on its own after the handshake
            self.replies.get_nowait()
        self.last_keepalive = time.monotonic()

    async def _keep_session(self) -> None:
        while self.notifications - self.granted >= CREDIT_STEP:
            await self.client.write_gatt_char(UUID_CONTROL, CREDIT_GRANT, response=False)
            self.granted += CREDIT_STEP
        if time.monotonic() - self.last_keepalive >= KEEPALIVE_INTERVAL:
            await self.client.write_gatt_char(UUID_REGISTERS, KEEPALIVE, response=False)
            self.last_keepalive = time.monotonic()

    async def _reply(self, reg: int, accept, deadline: float):
        """The next VALUE or STATUS for reg, skipping VALUEs that `accept` rejects as stale."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            message = await asyncio.wait_for(self.replies.get(), left)
            if message.reg != reg:
                continue  # the device also pushes unrequested values
            if message.kind == "STATUS" or accept is None or accept(message.value):
                return message

    async def _request(self, reg: int, payload: bytes, accept) -> bytes:
        # Writes aren't acknowledged and occasionally get lost. Every request is
        # idempotent, so a missing reply just means sending it again.
        for _ in range(TRIES):
            await self._keep_session()
            await self.client.write_gatt_char(UUID_REGISTERS, payload, response=False)
            try:
                message = await self._reply(reg, accept, time.monotonic() + REPLY_TIMEOUT)
            except TimeoutError:
                self.resends += 1
                continue
            if message.kind == "STATUS":
                raise DeviceStatus(reg, message.value)
            return message.value
        raise TimeoutError(f"no reply from register {reg:#06x} after {TRIES} tries")

    async def get(self, reg: int) -> bytes:
        return await self._request(reg, encode_get(reg), None)

    async def set(self, reg: int, payload: bytes, accept=None) -> bytes:
        check_cursor(reg)
        return await self._request(reg, encode_set(reg, payload), accept)


async def pull_device(address: str, attempts: int = 1, now: int | None = None) -> dict:
    """Connect to a device, read its Trends store, and disconnect.

    Only failed connection attempts are retried. A failure mid-session raises.
    """
    for attempt in range(1, attempts + 1):
        client = BleakClient(address, timeout=20)
        try:
            await client.connect()
        except (BleakError, TimeoutError, OSError) as exc:
            log.warning("connect attempt %d of %d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(3)
            continue
        try:
            session = BleSession(client)
            await session.start()
            started = time.monotonic()
            result = await pull(session, now=now)
            log.info("read %d blocks in %.1f s (%d lost requests re-sent)",
                     len(result["blocks"]), time.monotonic() - started, session.resends)
        finally:
            await client.disconnect()
        result["address"] = address
        return result
    raise ConnectionError(f"could not connect to {address} in {attempts} attempts")
