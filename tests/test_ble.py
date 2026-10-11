"""pull_device's handling of links that drop or stall, with a fake bleak client."""
import asyncio

import pytest

pytest.importorskip("bleak")

from victron_trends import ble  # noqa: E402


class FakeClient:
    """Each instance plays the next behaviour from `script`:
    "drop": connects, then disconnects while StartNotify is pending, which never returns
            (what BlueZ did on 2026-10-10)
    "stall": connects, then StartNotify never returns and the link stays up
    "ok": a working session"""
    script: list = []
    made: list = []

    def __init__(self, address, timeout=20, disconnected_callback=None):
        self.behaviour = FakeClient.script.pop(0)
        self.on_disconnect = disconnected_callback
        self.disconnected = False
        FakeClient.made.append(self)

    async def connect(self):
        return True

    async def start_notify(self, uuid, callback):
        if self.behaviour == "drop":
            asyncio.get_running_loop().call_later(0.05, self.on_disconnect, self)
            await asyncio.Event().wait()
        if self.behaviour == "stall":
            await asyncio.Event().wait()

    async def write_gatt_char(self, uuid, data, response=False):
        pass

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake(monkeypatch):
    FakeClient.made = []
    monkeypatch.setattr(ble, "BleakClient", FakeClient)
    monkeypatch.setattr(ble, "SETUP_TIMEOUT", 0.5, raising=False)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    async def fake_pull(session, now=None):
        return {"blocks": ["ab"]}
    monkeypatch.setattr(ble, "pull", fake_pull)
    return FakeClient


_real_sleep = asyncio.sleep


async def _fast_sleep(seconds, *args):
    await _real_sleep(min(seconds, 0.01), *args)


def test_link_dropped_during_setup_is_retried(fake):
    fake.script = ["drop", "ok"]
    result = asyncio.run(asyncio.wait_for(ble.pull_device("AA:BB", attempts=3), 5))
    assert result["blocks"] == ["ab"] and result["address"] == "AA:BB"
    assert len(fake.made) == 2 and all(c.disconnected for c in fake.made)


def test_stalled_setup_times_out_and_is_retried(fake):
    fake.script = ["stall", "ok"]
    result = asyncio.run(asyncio.wait_for(ble.pull_device("AA:BB", attempts=3), 5))
    assert result["blocks"] == ["ab"]


def test_gives_up_after_the_attempts_instead_of_hanging(fake):
    fake.script = ["drop", "stall", "drop"]
    with pytest.raises(ConnectionError, match="3 attempts"):
        asyncio.run(asyncio.wait_for(ble.pull_device("AA:BB", attempts=3), 5))
    assert all(c.disconnected for c in fake.made)


def test_until_lost():
    async def slow():
        await asyncio.sleep(10)

    async def quick():
        return 7

    async def run():
        lost = asyncio.Event()
        assert await ble.until_lost(quick(), lost, 1) == 7
        with pytest.raises(TimeoutError):
            await ble.until_lost(slow(), lost, 0.05)
        asyncio.get_running_loop().call_later(0.02, lost.set)
        with pytest.raises(ConnectionError):
            await ble.until_lost(slow(), lost, 5)
    asyncio.run(run())
