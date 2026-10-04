"""Extract the Trends register traffic of one VictronConnect connection from a btsnoop capture.

Only the Trends registers (0xEC4A-0xEC64) are kept. Everything else is dropped,
including 0xEC65 (very likely the Instant Readout key) and 0xEC66 (the device's
address), so the output is safe to publish.

    python tests/make_fixture.py <btsnoop.log> <address> <connection #> <out.json>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from victron_trends.capture import connections, register_traffic  # noqa: E402

TRENDS_REGISTERS = range(0xEC4A, 0xEC65)


def encode(messages, kinds):
    out = []
    for m in messages:
        if m.kind in kinds and isinstance(m.reg, int) and m.reg in TRENDS_REGISTERS:
            out.append([m.kind, m.reg, m.value.hex() if isinstance(m.value, bytes) else m.value])
    return out


def main(capture, address, connection, out):
    replies, requests = register_traffic(connections(capture, address)[int(connection) - 1])
    fixture = {"replies": encode(replies, {"VALUE", "STATUS"}), "requests": encode(requests, {"SET"})}
    Path(out).write_text(json.dumps(fixture, indent=0))
    print(f"{len(fixture['replies'])} replies, {len(fixture['requests'])} requests -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:])
