"""The reader against VictronConnect itself.

The fixture is the Trends traffic of one app session with a SmartShunt. The
reader walks it through ReplaySession exactly as it would a live device, and
the decoded samples must reproduce the 40 values the app showed on its Trends
screen for that session (read off screenshots: hourly tooltips and day headers).
"""
import asyncio
from datetime import datetime, timedelta, timezone

from victron_trends.decode import finest
from victron_trends.reader import CURSORS, pull, pull_samples

PDT = timezone(timedelta(hours=-7))  # the phone's time zone

HOURLY = [  # (hour starting, SoC %, Charge Ah)
    ("2026-08-30 09", 53.0, -0.20), ("2026-08-30 17", 42.5, -5.52), ("2026-08-30 21", 32.0, 0.00),
    ("2026-09-01 09", 81.5, 7.47), ("2026-09-01 11", 98.5, 4.24), ("2026-09-01 12", 100.0, 0.45),
    ("2026-09-01 21", 100.0, 0.00), ("2026-09-02 09", 100.0, 0.12), ("2026-09-02 13", 100.0, -0.25),
    ("2026-09-02 14", 99.5, -1.48), ("2026-09-02 21", 84.5, -1.67), ("2026-10-03 17", 80.0, 0.00),
]
DAILY = {  # day: (From battery Ah, To battery Ah, SoC min %, SoC max %)
    "2026-08-30": (-22, 0, 32.0, 55.0), "2026-09-01": (-8, 25, 79.0, 100.0),
    "2026-09-02": (-20, 1, 81.0, 100.0), "2026-10-03": (0, 0, 80.0, 80.0),
}
SOC, CURRENT = 0, 3  # series indexes on a SmartShunt


def local(text, fmt):
    return int(datetime.strptime(text, fmt).replace(tzinfo=PDT).timestamp())


def charge_ah(current, start, end):
    """Ah moved during [start, end). Each current sample is the mean over (t - interval, t]."""
    return sum(s.value / 1000 * max(0, min(s.time, end) - max(s.time - s.interval, start)) / 3600 for s in current)


def test_reader_reproduces_the_app_screen(app_session):
    result = asyncio.run(pull(app_session, now=app_session.app_time))
    assert set(app_session.writes) <= CURSORS
    # Every block the reader keeps is one the device sent the app. The app made 16
    # more requests (one sample just past each tier's head, 4 series x 4 tiers),
    # which the reader doesn't need.
    recorded = set(app_session.blocks.values())
    assert len(result["blocks"]) == 462
    assert all(bytes.fromhex(b) in recorded for b in result["blocks"])

    decoded = pull_samples(result)
    soc, current = finest(decoded, SOC), finest(decoded, CURRENT)

    mismatches, checked = [], 0

    def check(label, got, want, tolerance):
        nonlocal checked
        checked += 1
        if abs(got - want) > tolerance:
            mismatches.append(f"{label}: app {want}, decoded {got:.2f}")

    for hour, want_soc, want_ah in HOURLY:
        start = local(hour, "%Y-%m-%d %H")
        in_hour = [s.value for s in soc if start <= s.time < start + 3600]
        check(f"{hour}h SoC", sum(in_hour) / len(in_hour), want_soc, 0.05)
        check(f"{hour}h Charge", charge_ah(current, start, start + 3600), want_ah, 0.011)

    for day, (want_out, want_in, want_min, want_max) in DAILY.items():
        start = local(day, "%Y-%m-%d")
        hours = [charge_ah(current, h, h + 3600) for h in range(start, start + 86400, 3600)]
        in_day = [s.value for s in soc if start <= s.time < start + 86400]
        check(f"{day} From battery", round(sum(h for h in hours if h < 0)), want_out, 0)
        check(f"{day} To battery", round(sum(h for h in hours if h > 0)), want_in, 0)
        check(f"{day} SoC min", min(in_day), want_min, 0)
        check(f"{day} SoC max", max(in_day), want_max, 0)

    assert checked == 40
    assert not mismatches, mismatches
