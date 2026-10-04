import struct

from victron_trends.decode import Block, Sample, block_values, finest, parse_block, samples, segment_offset, sync_pairs


def block(series, ts, interval, fmt, values):
    body = b"".join(struct.pack(fmt, v) if isinstance(v, int) else v for v in values)
    return Block(series, ts, len(values), interval, body)


def test_parse_block():
    raw = struct.pack("<BIBH", 3, 0x1000, 2, 1800) + struct.pack("<ii", -1500, 200)
    assert parse_block(raw) == Block(3, 0x1000, 2, 1800, struct.pack("<ii", -1500, 200))
    assert parse_block(raw[:7]) is None


def test_samples_are_newest_first_and_skip_not_available():
    b = block(2, 10_000, 1800, "<h", [2650, b"\xff\x7f", 2640])
    assert list(block_values(b)) == [(10_000, 2650), (6_400, 2640)]


def test_one_byte_samples_are_unsigned():
    assert list(block_values(block(0, 100, 60, "<B", [200, b"\xff"]))) == [(100, 200)]


def test_history_padding():
    page = struct.pack("<II", 500, 1_700_000_000) + b"\xff" * 56
    assert sync_pairs(page) == [(500, 1_700_000_000)]


def test_each_boot_segment_uses_its_own_sync():
    boots = [1_000, 50_000]  # the clock stopped while unpowered and resumed at 50,000
    syncs = {2_000: 1_700_000_000, 60_000: 1_710_000_000}
    assert segment_offset(5_000, syncs, boots) == 1_700_000_000 - 2_000
    assert segment_offset(55_000, syncs, boots) == 1_710_000_000 - 60_000
    assert segment_offset(500, syncs, boots) is None  # before the first boot, no sync


def test_finest_prefers_the_finer_tier_where_they_overlap():
    fine = block(0, 10_000, 300, "<B", [80, 79, 78])        # 9,400 .. 10,000
    coarse = block(0, 10_000, 1800, "<B", [80, 75, 70])     # 6,400 .. 10,000
    got = finest(samples([fine, coarse], {0: 0}, []), 0)
    assert [(s.time, s.interval) for s in got] == [(6_400, 1800), (8_200, 1800), (9_400, 300), (9_700, 300), (10_000, 300)]
    assert got[-1] == Sample(0, 10_000, 80, 300)
