"""Reading a device's whole Trends store.

pull() works over any session object with two coroutines:

    get(reg) -> bytes                     a plain read
    set(reg, payload, accept) -> bytes    write a cursor; the device answers the write itself

Both raise DeviceStatus when the device answers with a status instead of a value.
`accept` lets the session drop late replies to earlier requests. ble.BleSession
implements this over bleak, capture.ReplaySession over a recorded VictronConnect
session.

pull() only writes CURSORS, which choose what the device sends back. It never
writes the time-sync register (0xEC5F) or any setting: it places the current
boot segment with this machine's clock instead.
"""
from __future__ import annotations

import logging
import struct
import time
from typing import Callable

from .decode import Sample, parse_block, parse_layout, parse_series_map, samples, sync_pairs, u32_list

REG_LAYOUT = 0xEC4A  # + series index
REG_HEAD = 0xEC52    # + series index
REG_CLOCK = 0xEC5A
REG_BLOCK = 0xEC5B
REG_SERIES_MAP = 0xEC5D
REG_TIME_SYNC = 0xEC5F  # VictronConnect writes this; this package never does
REG_SYNC_HISTORY = 0xEC63
REG_BOOT_HISTORY = 0xEC64
CURSORS = frozenset({REG_BLOCK, REG_SYNC_HISTORY, REG_BOOT_HISTORY})

SYNCS_PER_PAGE, BOOTS_PER_PAGE = 8, 16

log = logging.getLogger(__name__)

Accept = Callable[[bytes], bool]


class DeviceStatus(Exception):
    def __init__(self, reg: int, status: int):
        super().__init__(f"register {reg:#06x} answered with status {status}")
        self.reg, self.status = reg, status


def check_cursor(reg: int) -> None:
    if reg not in CURSORS:
        raise PermissionError(f"refusing to write register {reg:#06x}: not a Trends cursor")


async def _history(session, reg: int, now: int, per_page: int, entries: Callable[[bytes], list],
                   counter: Callable = lambda e: e) -> list:
    """Page through a history register from `now` back to its oldest entry.

    Each page holds entries at or below the cursor, newest first. A page answering
    an earlier, higher cursor would hold entries above the current one, so it's
    rejected as a late reply.
    """
    found, cursor = [], now
    while True:
        page = await session.set(reg, struct.pack("<I", cursor),
                                 accept=lambda v, cur=cursor: all(counter(e) <= cur for e in entries(v)))
        got = entries(page)
        found += got
        if len(got) < per_page:
            return found
        cursor = min(counter(e) for e in got) - 1


def _block_filter(series: int, ts: int, interval: int) -> Accept:
    """Reject late replies to earlier block requests.

    The walk goes series by series, finest tier to coarsest, newest block to oldest,
    so a stale reply is for another series, a finer tier, or a later time.
    """
    def accept(value: bytes) -> bool:
        block = parse_block(value)
        if block is None or block.count == 0:
            return True
        if block.series != series or block.interval < interval:
            return False
        return not (block.interval == interval and block.ts - ts >= interval)
    return accept


async def pull(session, now: int | None = None) -> dict:
    """Read every block of every series and tier, plus the clock and its history.

    `now` is the Unix time to pair with the device clock (defaults to this machine's
    clock). The result is JSON-serializable; pass it to pull_samples() to decode it.
    """
    started = time.monotonic()
    device_clock = struct.unpack("<I", await session.get(REG_CLOCK))[0]
    now = int(now if now is not None else time.time())
    series_map = parse_series_map(await session.get(REG_SERIES_MAP))

    layouts, heads = [], []
    for s in range(len(series_map)):
        per_block, tiers = parse_layout(await session.get(REG_LAYOUT + s))
        layouts.append({"samples_per_block": per_block, "tiers": tiers})
        head = await session.get(REG_HEAD + s)
        heads.append(list(struct.unpack_from(f"<{len(tiers)}I", head)))

    # A device without these registers answers with a status. Its samples are then
    # placed by local_syncs alone.
    try:
        syncs = await _history(session, REG_SYNC_HISTORY, device_clock, SYNCS_PER_PAGE, sync_pairs,
                               counter=lambda pair: pair[0])
    except DeviceStatus:
        syncs = []
    try:
        boots = await _history(session, REG_BOOT_HISTORY, device_clock, BOOTS_PER_PAGE, u32_list)
    except DeviceStatus:
        boots = []

    blocks = []
    for s, layout in enumerate(layouts):
        per_block = layout["samples_per_block"]
        for tier, (tier_len, interval) in enumerate(layout["tiers"]):
            ts, remaining = heads[s][tier], tier_len
            while remaining > 0:
                try:
                    reply = await session.set(REG_BLOCK, struct.pack("<BIB", s, ts, per_block),
                                              accept=_block_filter(s, ts, interval))
                except TimeoutError as exc:
                    raise TimeoutError(f"{exc} (series {s}, tier {tier}, ts {ts:#x}, "
                                       f"{len(blocks)} blocks read so far)") from None
                block = parse_block(reply)
                # An empty block, or one from a coarser tier, means this tier is exhausted.
                if block is None or block.count == 0 or block.interval != interval:
                    break
                if not 0 <= block.ts - ts < interval:
                    raise RuntimeError(f"asked for a block at {ts:#x}, got one at {block.ts:#x}")
                blocks.append(reply.hex())
                remaining -= block.count
                if block.count < per_block:
                    break  # a short block is the tier's oldest
                ts -= per_block * interval
            log.info("series %d tier %d (%d s): %d blocks so far, %.1f s",
                     s, tier, interval, len(blocks), time.monotonic() - started)

    return {
        "pulled_at": now,
        "device_clock": device_clock,
        "series_map": series_map,
        "layouts": layouts,
        "heads": heads,
        "syncs": sorted(set(syncs)),
        # Our own (device counter, Unix time) pair. Keep these across pulls: they're
        # what places a boot segment in which the app never synced.
        "local_syncs": [(device_clock, now)],
        "boots": sorted(set(boots)),
        "blocks": blocks,
    }


def pull_samples(result: dict) -> list[Sample]:
    """Decode a pull() result into samples in real time."""
    syncs = dict(map(tuple, result["syncs"]))
    syncs.update(dict(map(tuple, result["local_syncs"])))
    blocks = [parse_block(bytes.fromhex(h)) for h in result["blocks"]]
    return samples(blocks, syncs, result["boots"])
