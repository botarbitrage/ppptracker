"""
pokerpulse_scheduler.py — pure date/classification math for PokerPulse's
automated cron run (F2-1).

Kept separate from app.py so the DST-sensitive Adelaide cutoff/window math
can be unit tested (test_pokerpulse_scheduler.py) without touching
Firestore, Flask, or the network. Every function here takes and returns
tz-aware datetimes; app.py converts to/from epoch-second ints (the rest of
the codebase's convention for window_start/window_end) at the boundary,
same split as session_engine.py/highlights.py vs. their app.py callers.

Same lesson as hand_exporter.py's `_ADELAIDE_TZ`: always ZoneInfo, never a
fixed +9:30/+10:30 offset — Adelaide switches to daylight saving (ACDT,
UTC+10:30) on the first Sunday of October and back (ACST, UTC+9:30) on the
first Sunday of April, so a fixed offset silently mis-lands the 5:00am
boundary for roughly half the year.
"""
from datetime import datetime, timedelta, time as _time
from zoneinfo import ZoneInfo

ADELAIDE_TZ = ZoneInfo('Australia/Adelaide')

WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

# Settings doc defaults — see config/pokerpulse in docs/firestore-schema.md.
# `enabled` defaults OFF: this is a kill switch, the automated run must never
# start sending on its own just because the doc doesn't exist yet.
POKERPULSE_SETTINGS_DEFAULTS = {
    'enabled':               False,
    'send_time':             '05:15',
    'daily_threshold_hands': 100,
    'lookback_days':         3,
    'weekly_day':            'Monday',
}

CUTOFF_HOUR = 5  # 5:00am Adelaide — same boundary as the F1 Pulse-session spec.


def most_recent_cutoff(now):
    """The most recent 5:00am Australia/Adelaide boundary at or before `now`
    (any tz-aware datetime — converted to Adelaide first).

    DST-correct by construction: the candidate boundary is built from `now`'s
    *Adelaide calendar date*, then re-derived on the previous calendar date
    if that candidate is still in the future — never a fixed-hours
    subtraction, which would land on the wrong wall-clock time across the
    ACST/ACDT switch.
    """
    now_adl = now.astimezone(ADELAIDE_TZ)
    cutoff = now_adl.replace(hour=CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if now_adl < cutoff:
        prev_date = (now_adl - timedelta(days=1)).date()
        cutoff = datetime(prev_date.year, prev_date.month, prev_date.day,
                           CUTOFF_HOUR, 0, 0, tzinfo=ADELAIDE_TZ)
    return cutoff


def cutoff_date_str(cutoff):
    """YYYY-MM-DD Adelaide calendar date of a cutoff — the pokerpulse_runs/
    {date} idempotency bucket key."""
    return cutoff.date().isoformat()


def should_process_now(now, send_time_str):
    """False before today's send_time (Adelaide wall-clock) — the cron tick
    is a pure no-op until then, per this task's AC. `send_time_str` is
    'HH:MM'."""
    now_adl = now.astimezone(ADELAIDE_TZ)
    hh, mm = (int(p) for p in send_time_str.split(':'))
    return now_adl.timetz().replace(tzinfo=None) >= _time(hh, mm)


def is_weekly_send_day(cutoff, weekly_day_str):
    """True if `cutoff`'s Adelaide weekday matches the configured
    weekly_day (e.g. 'Monday')."""
    return WEEKDAY_NAMES[cutoff.weekday()] == weekly_day_str


def classify(hand_count_in_lookback, daily_threshold_hands):
    """'daily' if the subscriber logged >= daily_threshold_hands hands in the
    classification lookback window, else 'weekly'. Recomputed every run —
    nothing about a subscriber's bucket is persisted between ticks."""
    return 'daily' if hand_count_in_lookback >= daily_threshold_hands else 'weekly'


def classification_lookback_window(cutoff, lookback_days):
    """[cutoff − lookback_days, cutoff) — the window whose hand count decides
    daily vs. weekly."""
    return cutoff - timedelta(days=lookback_days), cutoff


def daily_window(cutoff):
    """[cutoff − 24h, cutoff) — a DAILY subscriber's report window."""
    return cutoff - timedelta(hours=24), cutoff


def weekly_window(cutoff, last_sent_window_end):
    """[max(cutoff − 7d, last_sent_window_end), cutoff) — a WEEKLY
    subscriber's report window. `last_sent_window_end` is the end of that
    user's last *sent* report (tz-aware datetime), or None if they've never
    been sent one; this is what keeps a user who moves from daily to weekly
    from ever getting the same hands twice."""
    floor = cutoff - timedelta(days=7)
    start = max(floor, last_sent_window_end) if last_sent_window_end else floor
    return start, cutoff


def to_epoch(dt):
    """tz-aware datetime -> epoch-second int, the codebase-wide
    window_start/window_end representation."""
    return int(dt.timestamp())


def from_epoch(ts, tz=ADELAIDE_TZ):
    """epoch-second int -> tz-aware datetime (Adelaide by default)."""
    return datetime.fromtimestamp(ts, tz=tz)
