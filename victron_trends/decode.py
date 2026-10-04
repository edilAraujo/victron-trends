"""Decoding Trends data: blocks, the device clock, and what each series measures.

A block (the reply to a 0xEC5B cursor write) is

    series:u8  ts:u32  count:u8  interval_s:u16  samples[count]

with samples newest first: sample i is at device time ts - i * interval_s and
covers (t - interval_s, t].

The device clock counts seconds but stops while the device is unpowered, so
device time maps to real time per boot segment: boot counters (0xEC64) split it
into segments, and a time sync (0xEC63) taken inside a segment gives that
segment's offset to Unix time.
"""
from __future__ import annotations

import struct
from collections import defaultdict
from typing import Iterable, Iterator, NamedTuple

EMPTY_U32 = 0xFFFFFFFF

# What each series measures, keyed by its source register (listed in 0xEC5D).
# Series indexes differ between device types, so always go through the register.
SERIES = {
    0xEC87: ("soc", "%", 1),                 # SmartShunt
    0xED8D: ("battery_voltage", "V", 100),   # SmartShunt, SmartSolar
    0xED8C: ("battery_current", "A", 1000),  # SmartShunt; negative while discharging
    0xEDBB: ("pv_voltage", "V", 100),        # SmartSolar
    0xEC8A: ("pv_power", "W", 1),            # SmartSolar
    0xED8F: ("charge_current", "A", 10),     # SmartSolar
}

# "Not available" sample values, by sample size in bytes.
NOT_AVAILABLE = {
    1: {b"\xff"},
    2: {b"\xff\xff", b"\xff\x7f"},
    4: {b"\xff\xff\xff\x7f", b"\xff\xff\xff\xff"},
}


class Block(NamedTuple):
    series: int
    ts: int
    count: int
    interval: int
    body: bytes


class Sample(NamedTuple):
    series: int
    time: int  # Unix time at the end of the interval the value covers
    value: int  # raw; divide by the series' divisor
    interval: int


def parse_block(data: bytes) -> Block | None:
    if len(data) < 8:
        return None
    series, ts, count, interval = struct.unpack("<BIBH", data[:8])
    return Block(series, ts, count, interval, bytes(data[8:]))


def u32_list(data: bytes) -> list[int]:
    """Little-endian u32s with the 0xFFFFFFFF padding removed."""
    return [v for (v,) in struct.iter_unpack("<I", data[:len(data) // 4 * 4]) if v != EMPTY_U32]


def sync_pairs(data: bytes) -> list[tuple[int, int]]:
    """(device counter, Unix time) pairs from a 0xEC5F or 0xEC63 value."""
    pairs = struct.iter_unpack("<II", data[:len(data) // 8 * 8])
    return [(c, u) for c, u in pairs if c != EMPTY_U32]


def parse_layout(data: bytes) -> tuple[int, list[tuple[int, int]]]:
    """0xEC4A+n: (samples per block, [(tier length, interval_s), ...])."""
    n_tiers, per_block = data[0], data[1]
    tiers = [struct.unpack_from("<HH", data, 2 + 4 * k) for k in range(n_tiers)]
    return per_block, tiers


def parse_series_map(data: bytes) -> list[int]:
    """0xEC5D: the source register of each series, in series order."""
    return [r for (r,) in struct.iter_unpack("<H", data[:len(data) // 2 * 2]) if r != 0xFFFF]


def block_values(block: Block) -> Iterator[tuple[int, int]]:
    """(device time, raw value) for each available sample in a block.

    The sample size follows from the block itself. 1-byte samples are unsigned,
    2- and 4-byte samples signed.
    """
    if block.count == 0 or len(block.body) % block.count:
        return
    size = len(block.body) // block.count
    fmt = {1: "<B", 2: "<h", 4: "<i"}.get(size)
    if fmt is None:
        return
    for i in range(block.count):
        raw = block.body[i * size:(i + 1) * size]
        if raw not in NOT_AVAILABLE[size]:
            yield block.ts - i * block.interval, struct.unpack(fmt, raw)[0]


def segment_offset(counter: int, syncs: dict[int, int], boots: Iterable[int]) -> int | None:
    """Unix time minus device time for the boot segment holding `counter`, if a sync places it."""
    boots = tuple(boots)
    start = max((b for b in boots if b <= counter), default=0)
    end = min((b for b in boots if b > counter), default=float("inf"))
    inside = [c for c in syncs if start <= c < end]
    if not inside:
        return None
    latest = max(inside)
    return syncs[latest] - latest


def samples(blocks: Iterable[Block], syncs: dict[int, int], boots: Iterable[int]) -> list[Sample]:
    """Every sample whose boot segment can be placed in real time, sorted by series and time.

    Tiers overlap, so the same moment can appear at several intervals; see finest().
    """
    boots = tuple(boots)
    raw = {}
    for block in blocks:
        for device_ts, value in block_values(block):
            raw[(block.series, device_ts, block.interval)] = value
    offsets = {}
    out = []
    for (series, device_ts, interval), value in raw.items():
        if device_ts not in offsets:
            offsets[device_ts] = segment_offset(device_ts, syncs, boots)
        offset = offsets[device_ts]
        if offset is not None:
            out.append(Sample(series, device_ts + offset, value, interval))
    return sorted(out)


def finest(all_samples: Iterable[Sample], series: int) -> list[Sample]:
    """One sample per moment for a series, preferring the finest tier where tiers overlap."""
    by_interval = defaultdict(list)
    for s in all_samples:
        if s.series == series:
            by_interval[s.interval].append(s)
    out, covered_from = [], float("inf")
    for interval in sorted(by_interval):
        tier = sorted(by_interval[interval], key=lambda s: s.time)
        out += [s for s in tier if s.time <= covered_from]
        covered_from = min(covered_from, tier[0].time - interval)
    return sorted(out, key=lambda s: s.time)
