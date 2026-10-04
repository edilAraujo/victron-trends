"""Read and decode the Trends history stored on Victron devices."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import logging
import sys
from datetime import datetime, timezone

from .decode import SERIES, finest
from .reader import pull, pull_samples


def write_json(result: dict, path: str | None) -> None:
    text = json.dumps(result)
    if path:
        with open(path, "w") as f:
            f.write(text)
    else:
        print(text)


def cmd_pull(args) -> None:
    from .ble import pull_device  # needs bleak

    write_json(asyncio.run(pull_device(args.address, attempts=args.attempts)), args.output)


def cmd_replay(args) -> None:
    from .capture import ReplaySession

    session = ReplaySession.from_capture(args.capture, args.address, args.connection - 1)
    write_json(asyncio.run(pull(session, now=session.app_time)), args.output)


def cmd_csv(args) -> None:
    with open(args.pull) as f:
        result = json.load(f)
    samples = pull_samples(result)
    columns, rows = [], {}
    for index, reg in enumerate(result["series_map"]):
        name, _unit, divisor = SERIES.get(reg, (f"reg_{reg:04x}_raw", "", 1))
        columns.append(name)
        for s in finest(samples, index):
            rows.setdefault((s.time, s.interval), {})[name] = s.value / divisor
    with open(args.output, "w", newline="") if args.output else contextlib.nullcontext(sys.stdout) as out:
        writer = csv.writer(out)
        writer.writerow(["time_utc", "interval_s"] + columns)
        for (t, interval), values in sorted(rows.items()):
            when = datetime.fromtimestamp(t, timezone.utc).isoformat()
            writer.writerow([when, interval] + [values.get(c, "") for c in columns])


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="victron-trends", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pull", help="read a device's Trends store over BLE and write it as JSON")
    p.add_argument("address", help="device Bluetooth address (or a CoreBluetooth UUID on macOS)")
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.add_argument("--attempts", type=int, default=5, help="connection attempts (default: 5)")
    p.set_defaults(run=cmd_pull)

    p = sub.add_parser("replay", help="run the reader against a VictronConnect session in a btsnoop capture")
    p.add_argument("capture")
    p.add_argument("address")
    p.add_argument("--connection", type=int, default=1, help="which connection to that device, from 1 (default: 1)")
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(run=cmd_replay)

    p = sub.add_parser("csv", help="convert a pull JSON file to CSV, finest tier at each moment")
    p.add_argument("pull")
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(run=cmd_csv)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    args.run(args)


if __name__ == "__main__":
    main()
