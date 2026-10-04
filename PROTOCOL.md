# Victron Trends over BLE

Victron SmartShunts and SmartSolar chargers keep about 45 days of history on the device, which VictronConnect shows on its Trends screen. This is how to read it.

It was worked out from Android Bluetooth HCI captures of VictronConnect talking to two SmartShunts (300A and 500A), and confirmed against a SmartSolar MPPT 100/50 with a live reader, in October 2026. The reader in this repository reproduces all 40 values read off the app's Trends screen for one of those sessions (`tests/test_app_screen.py`). Items marked *unconfirmed* are inferred, not proven.

## 1. Transport

Everything runs over Victron's vendor service `306b0001-b081-4037-83dc-e59fcc3cdfd0`.

| Characteristic | Handle seen | Role |
|---|---|---|
| `306b0002` | 0x0021 | Session setup and notification flow control (1.1) |
| `306b0003` | 0x0024 | Register channel |
| `306b0004` | 0x0027 | Register channel |

**`306b0003` and `306b0004` carry one byte stream in each direction.** The app splits its writes across both, and messages span write boundaries. Notifications from both must be concatenated in arrival order before parsing. In one capture, decoding the two separately left 3,351 bytes undecodable; merged, 21 of 40,755 bytes were.

The phone requests an MTU of 512 and the shunts answer 77, so a notification carries at most 74 bytes. In a btsnoop log some notifications are split across HCI ACL continuation fragments, which have to be reassembled. A live BLE stack does that itself.

Every device here was bonded (paired with its PIN) before reading. Whether that's required is untested (section 9).

### 1.1 Keeping a session alive

Without both of these, the device stops answering partway through a download.

- **Flow control on `306b0002`.** The client's `f9 80` at session start lets the device send 128 notifications on the register channel. Each `f9 41` from the client allows 65 more. When the allowance runs out the device goes silent but keeps the link open. VictronConnect sends `f9 41` after every 65 notifications. The device sends `f9 01` on `306b0002` (meaning *unconfirmed*).
- **Keep-alive.** SET register 0x93 to 10000 (`06 00 82 18 93 42 10 27`). The device drops the link about 10 s after the last one, so VictronConnect re-sends it at least every 3.5 s.

The session starts with `fa 80 ff` and `f9 80` on `306b0002`, then `01`, `03 00` and the keep-alive (followed by `03 01 03 03`) on `306b0003`. [patlux/ve-smart-telemetry](https://github.com/patlux/ve-smart-telemetry)'s static analysis of the app names `fa 80 ff` as `writeCborChunkSize(0x80, 0xff)`. The other bytes are copied as-is; their meaning is unknown.

After the handshake the device also pushes unrequested values about once a second (0xEC5A clock, 0xED8D voltage, 0x0308), so replies have to be matched by register.

## 2. Message framing

Messages are a 2-byte prefix followed by CBOR items:

| Direction | Bytes | Meaning |
|---|---|---|
| client to device | `05 <sub> [reg, ...]` | GET: CBOR array of register ids |
| client to device | `06 <sub> [reg, value]` | SET: CBOR array of 2 |
| device to client | `08 <sub> reg value` | VALUE |
| device to client | `09 <sub> reg status` | STATUS (CBOR int) |

- Register ids are CBOR unsigned integers: `19 EC 5B` is 0xEC5B, small ids use `18 xx`.
- Values are CBOR byte strings: `40`–`57` carry the length inline, `58 LL` a 1-byte length. Integers inside them are little-endian.
- These are VE.Direct registers: 0xED8D is battery voltage, 0xED8C battery current. The lifetime history values (0x03xx) use the same framing.
- `<sub>` is `03` for all Trends traffic. Its meaning is *unconfirmed*.
- STATUS values seen: `3` for a plain GET of a cursor register (0xEC5B, 0xEC63, 0xEC64), which the app sends after each cursor write and ignores; `-5` for unsupported registers (0xEC4E–51 and 0xEC56–59 on a SmartShunt); `1` for 0xEC5C. Exact meanings are *unconfirmed*.
- **Writing a cursor register is the request.** The device answers the SET with a VALUE carrying the selected page or block. No GET is needed.

## 3. Registers

| Register | Access | Payload | Meaning |
|---|---|---|---|
| 0xEC4A + n | GET | 34 B | Tier layout for series n: `n_tiers:u8, samples_per_block:u8`, then `n_tiers` × `(tier_len:u16, interval_s:u16)`, `FF` padding |
| 0xEC52 + n | GET | 32 B | Newest sample time of each tier for series n, `u32` × `n_tiers`, `FF` padding |
| 0xEC5A | GET | 4 B | Device clock: a seconds counter, `u32` |
| 0xEC5B | SET | SET 6 B, reply up to 64 B | Block cursor (section 4) |
| 0xEC5D | GET | 16 B | Source register of each series, `u16` each, `FFFF` padding. The number of entries is the number of series. |
| 0xEC5F | GET / SET | 8 B | Latest time sync `(counter:u32, unix:u32)`. The app writes the current pair on every connect. |
| 0xEC63 | SET | SET 4 B, reply 64 B | Sync history: up to 8 `(counter, unix)` pairs with counter ≤ cursor, newest first, `FF` fill |
| 0xEC64 | SET | SET 4 B, reply 64 B | Boot history: up to 16 boot counters (`u32`) ≤ cursor, newest first, `FF` fill |
| 0xEC65 | GET | 16 B | *Unconfirmed*, very likely the Instant Readout key: the app reads it together with 0xEC66 on every connect, and its Encryption data screen shows exactly a key and a MAC. Not read during this work. Treat it as a secret. |
| 0xEC66 | GET | 6 B | The device's Bluetooth address |

## 4. Blocks (0xEC5B)

**Request (SET):** `series:u8, ts:u32, count:u8`. A reader asks for one block's worth (`count` = samples per block).

**Reply (VALUE):** `series:u8, ts:u32, count:u8, interval_s:u16, samples[count]`

- Samples are **newest first**. Sample `i` is at device time `ts - i * interval_s`.
- Each sample covers `(t - interval_s, t]`. For current and power it's the average over that interval, which is what makes the app's hourly Ah values come out exactly.
- A block is at most 64 bytes: an 8-byte header and 56 bytes of samples. The sample size follows from the samples per block: 56 means 1-byte samples, 28 means 2-byte, 14 means 4-byte. 1-byte samples are unsigned, 2- and 4-byte samples signed.
- The device snaps `ts` to its own sample grid, at or after the requested time, so a reply can differ from the request by up to one interval.
- `count` is smaller than requested at the end of a tier. `count` = 0 means no data.

Series indexes mean different things on different devices. Always read 0xEC5D first and go by the source register.

### SmartShunt

| Series | Source reg | Type | Scale | Not available | Meaning |
|---|---|---|---|---|---|
| 0 | 0xEC87 | u8 | 1 % | `FF` | State of charge |
| 1 | 0xEC86 | 16-bit | ? | `FFFF` | *Unconfirmed*, likely the aux input. Always n/a on the tested shunts, which have aux disabled. |
| 2 | 0xED8D | s16 | 0.01 V | `7FFF` | Battery voltage |
| 3 | 0xED8C | s32 | 0.001 A | `7FFFFFFF` | Battery current, negative while discharging |

### SmartSolar MPPT

| Series | Source reg | Type | Scale | Not available | Meaning |
|---|---|---|---|---|---|
| 0 | 0xEC89 | u8 | ? | `FF` | *Unconfirmed*. Every sample was `FF` on the tested unit. |
| 1 | 0xEDBB | u16 | 0.01 V | `FFFF` | Panel (PV) voltage |
| 2 | 0xEC8A | u16 | 1 W | `FFFF` | Panel (PV) power, averaged over the interval |
| 3 | 0xEC88 | 8-bit | ? | `7F` | *Unconfirmed*. Always `7F`, possibly a temperature input that isn't fitted. |
| 4 | 0xED8D | s16 | 0.01 V | `7FFF` | Battery voltage |
| 5 | 0xED8F | s16 | 0.1 A | `7FFF` | Battery charge current |

The charger's battery-side power is charge current × battery voltage. With a SmartShunt on the same battery, that minus the shunt's net battery power gives the DC loads.

### Tiers

Identical for every series on both device types:

| Tier | Samples | Interval | Span |
|---|---|---|---|
| 0 | 120 | 1 s | 2 min |
| 1 | 239 | 30 s | ~2 h |
| 2 | 288 | 5 min | 1 day |
| 3 | 2,160 | 30 min | 45 days |

Where tiers overlap, use the finer one.

## 5. Time model

The device clock (0xEC5A) counts seconds but **stops while the device is unpowered** and resumes at boot. There's no real-time clock, so absolute time comes from syncs the app writes:

1. Boot counters (0xEC64) split device time into segments `[boot_i, boot_i+1)`.
2. A segment's offset is `unix - counter` for a sync (0xEC63) whose counter falls in that segment.
3. A sample's real time is its counter plus its segment's offset.

A segment with no sync can't be placed, but none was seen: the app syncs within seconds of every boot, because a boot usually means someone just reconnected power and opened the app. A reader that doesn't write syncs can place the current segment by pairing the clock with its own time, and should keep those pairs, since a later boot leaves that segment without one.

**Example.** On one of the shunts, late-August data sits in the segment that starts at boot counter 2,156,401, with offset 1,784,596,483 from a sync on 2026-08-15 00:14 UTC. The next boot is at counter 4,090,321, so power was lost at about 2026-09-06 09:27 UTC; it came back with a sync on 2026-09-18 04:05 UTC. Ignoring the boot, that data would land about 11.8 days late.

## 6. Reading procedure

1. Subscribe to notifications on all three characteristics and merge `306b0003`/`306b0004` into one stream. Send the handshake (1.1), then keep flow control and the keep-alive going for the whole session.
2. GET 0xEC5A, 0xEC5D, then 0xEC4A + n and 0xEC52 + n for each series.
3. SET 0xEC63 = current clock; the reply is the first page. Repeat with cursor = oldest counter returned − 1 until a page holds fewer than 8 entries. Same for 0xEC64, 16 per page.
4. For each series and tier: start at the tier's head from 0xEC52 + n. SET 0xEC5B `(series, ts, samples_per_block)` and take the block from the reply, then `ts -= samples_per_block * interval`. Stop when the tier is covered, a block comes back short or empty, or a block has a different interval: that means the request ran past the tier and the device answered from the next coarser one.

All of these writes go to cursor registers, which only choose what the device sends back. VictronConnect also SETs 0xEC5F to the current `(counter, unix time)` pair on every connect, which adds an entry to the device's sync history. That's the only write in the procedure that changes stored state, and a reader can skip it (section 5). The app also asks for one sample just past each tier's head before walking it (16 requests on a SmartShunt); a reader doesn't need them.

A full download is 462 blocks from a SmartShunt and 438 from a SmartSolar. The app pipelines its requests and finishes in about 4 seconds. The reader here waits for each reply and takes about 10.

## 7. How the app builds the Trends screen

All 40 values in the test were matched this way:

- **Hourly SoC:** mean of the SoC samples whose time falls in the hour.
- **Hourly Charge (Ah):** sum over current samples of `current × overlap((t - interval, t], hour) / 3600`.
- **Day "From battery" / "To battery":** sums of the negative and positive hourly Charge values, rounded to whole Ah.
- **Day SoC min/max:** min and max SoC sample in the day.

Times are the phone's local time.

## 8. Validation

- 40 of 40 values read off the app's Trends screen (12 hourly SoC, 12 hourly Charge, 4 days × From/To/min/max) match the decoded data of the same session.
- A live pull of the second shunt shared 6,417 sample timestamps with the app's capture from the same morning, with no value differences.
- SmartSolar: per-day panel energy integrated from the 30-minute power series agreed with the charger's own daily yield records to within about 5% on six of seven days. On the seventh the daily record claimed 8.14 kWh, against 3.18 kWh integrated, with an 848 W peak that day; the daily record is the one that's wrong.

## 9. Open questions

- Whether bonding is required. Every device here was paired first. [trevore/victron-ble-gatt](https://github.com/trevore/victron-ble-gatt) reports reading other registers on a SmartSolar and a BMV-712 without pairing.
- STATUS code meanings, the `<sub>` byte, and the handshake bytes `01`, `03 00` and `03 01 03 03`.
- What SmartShunt series 1 (0xEC86) and SmartSolar series 0 and 3 (0xEC89, 0xEC88) record.
- 0xEC5C, which answers with status 1.
- Why the app requests one sample past each tier's head.
- Other devices: BMV-71x, Smart Battery Sense, Orion, inverters.
