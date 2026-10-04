import json
from pathlib import Path

import pytest

from victron_trends.capture import ReplaySession
from victron_trends.framing import Message

FIXTURES = Path(__file__).parent / "fixtures"


def load_session(name: str) -> ReplaySession:
    """A ReplaySession from a fixture written by make_fixture.py."""
    data = json.loads((FIXTURES / name).read_text())

    def messages(rows):
        return [Message(kind, 0x03, reg, bytes.fromhex(v) if isinstance(v, str) else v) for kind, reg, v in rows]

    return ReplaySession(messages(data["replies"]), messages(data["requests"]))


@pytest.fixture
def app_session() -> ReplaySession:
    """A SmartShunt 300A (12V LiFePO4) as VictronConnect read it on 2026-10-03."""
    return load_session("smartshunt_app_session.json")
