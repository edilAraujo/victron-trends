# victron-trends

Read the history that Victron SmartShunts and SmartSolar chargers store on the device, the data behind VictronConnect's **Trends** screen, over Bluetooth LE.

Each device keeps about 45 days of state of charge, voltage and current (SmartShunt) or panel voltage, panel power, battery voltage and charge current (SmartSolar), in tiers from 1-second to 30-minute resolution. This reads all of it in about ten seconds, places it in real time, and writes JSON or CSV.

[PROTOCOL.md](PROTOCOL.md) describes the protocol: registers, block format, the device clock, and what keeps a session alive. As far as I know it wasn't documented anywhere before.

> **Unofficial.** Not affiliated with or endorsed by Victron Energy. "Victron", "SmartShunt", "SmartSolar" and "VictronConnect" are Victron Energy's trademarks.

## Status

Tested on a SmartShunt 300A, a SmartShunt 500A and a SmartSolar MPPT 100/50, from Linux (BlueZ, Raspberry Pi 4) with Python 3.13. The devices were paired with their PIN beforehand. Other models with a Trends screen probably work; reports welcome.

## Install

```
pip install "victron-trends[ble] @ git+https://github.com/edilAraujo/victron-trends"
```

The `ble` extra pulls in [bleak](https://github.com/hbldh/bleak). Decoding saved data needs nothing beyond Python 3.11.

## Use

```
victron-trends pull AA:BB:CC:DD:EE:FF -o shunt.json   # read the device
victron-trends csv shunt.json -o shunt.csv             # one row per moment, finest tier available
```

```python
import asyncio
from victron_trends import SERIES, finest, pull_samples
from victron_trends.ble import pull_device

result = asyncio.run(pull_device("AA:BB:CC:DD:EE:FF", attempts=5))
samples = pull_samples(result)
for index, reg in enumerate(result["series_map"]):
    name, unit, divisor = SERIES.get(reg, (hex(reg), "", 1))
    history = finest(samples, index)
    if history:  # a series can be entirely "not available"
        print(name, history[-1].value / divisor, unit)
```

`pull()` in `victron_trends.reader` only needs an object with async `get(reg)` and `set(reg, payload, accept)`, so it runs over any transport. `capture.ReplaySession` is one such object, built from a recorded VictronConnect session.

## What it writes to the device

Reading Trends means writing *cursor* registers (0xEC5B, 0xEC63, 0xEC64), which select which block or history page the device sends back. Besides those, it sends only the session handshake, keep-alive and flow-control messages VictronConnect sends. Any other register write is refused in code. It never writes settings, and unlike VictronConnect it doesn't write the time-sync register (0xEC5F): it pairs the device clock with your computer's clock instead. Keep the `local_syncs` from each pull if you plan to decode several pulls together (see PROTOCOL.md, section 5).

## How it's checked

- `tests/test_app_screen.py` runs the reader against the Trends traffic of a real VictronConnect session and checks the result against 40 values read off the app's Trends screen for that session: hourly state of charge and Ah, and each day's totals and extremes.
- A live pull from a second shunt matched the app's own download of the same morning on 6,417 sample timestamps, with no differences.

```
pip install -e ".[test]" && pytest
```

## Captures

To work on devices or registers not covered here, Android's Bluetooth HCI snoop log of a VictronConnect session is the starting point. `victron-trends replay <btsnoop.log> <address>` runs the reader against one.

**Never publish a raw capture.** It can contain pairing keys, and VictronConnect reads register 0xEC65, which is almost certainly the device's Instant Readout key, on every connect. `tests/make_fixture.py` extracts just the Trends registers into a file that's safe to share.

## Prior work

- [vvvrrooomm/victron](https://github.com/vvvrrooomm/victron) and [birdie1/victron](https://github.com/birdie1/victron) worked out the vendor GATT service, its message framing and the lifetime history values. The session handshake here follows theirs.
- [patlux/ve-smart-telemetry](https://github.com/patlux/ve-smart-telemetry)'s static analysis of VictronConnect independently names the same GET/SET/VALUE/STATUS messages and the keep-alive.
- [trevore/victron-ble-gatt](https://github.com/trevore/victron-ble-gatt) reads settings and live values over the same service.
- [keshavdv/victron-ble](https://github.com/keshavdv/victron-ble) decodes the Instant Readout advertisements, the live data the devices broadcast without a connection.

## License

MIT
