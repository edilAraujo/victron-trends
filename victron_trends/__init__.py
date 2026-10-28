"""Read the Trends history stored on Victron SmartShunts and SmartSolar chargers over BLE.

The protocol is described in PROTOCOL.md. ble.pull_device() reads a device;
reader.pull_samples() turns the result into timestamped samples. The ble module
needs bleak; everything else is pure Python.
"""
from .decode import SERIES, Sample, finest
from .reader import pull, pull_samples

__all__ = ["SERIES", "Sample", "finest", "pull", "pull_samples"]
__version__ = "0.1.3"
