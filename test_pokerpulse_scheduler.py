"""
test_pokerpulse_scheduler.py — pure-logic tests for pokerpulse_scheduler.py
(F2-1's DST-sensitive cutoff/classification/window math), no Firestore or
Flask involved.

Covers: daily-threshold boundary (99/100/101), the most-recent-cutoff and
send_time gate across the Adelaide DST switch (ACST -> ACDT on the first
Sunday of October, ACDT -> ACST on the first Sunday of April), and the
weekly window's overlap-trimming (a user who moves from daily to weekly
must never get the same hands twice).

    python test_pokerpulse_scheduler.py
"""
import sys
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

import pokerpulse_scheduler as S

ADL = S.ADELAIDE_TZ
UTC = ZoneInfo('UTC')

problems = []


def check(label, cond, detail=''):
    if not cond:
        problems.append(label + (' — ' + detail if detail else ''))


def adl(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=ADL)


# ── 1. Daily-threshold boundary (99/100/101) ────────────────────────────────
check('99 hands classifies weekly', S.classify(99, 100) == 'weekly')
check('100 hands classifies daily (>= is the operator)', S.classify(100, 100) == 'daily')
check('101 hands classifies daily', S.classify(101, 100) == 'daily')
check('0 hands classifies weekly', S.classify(0, 100) == 'weekly')

# ── 2. most_recent_cutoff — ordinary (non-DST-boundary) days ───────────────
# Just after 5am -> today's 5am cutoff.
c = S.most_recent_cutoff(adl(2026, 6, 15, 5, 0, 1))
check('just after 5am -> today 5am cutoff', c == adl(2026, 6, 15, 5, 0, 0), str(c))

# Exactly 5:00:00 -> today's cutoff (boundary is inclusive of "at or before").
c = S.most_recent_cutoff(adl(2026, 6, 15, 5, 0, 0))
check('exactly 5am -> today 5am cutoff', c == adl(2026, 6, 15, 5, 0, 0), str(c))

# Just before 5am -> yesterday's 5am cutoff.
c = S.most_recent_cutoff(adl(2026, 6, 15, 4, 59, 59))
check('just before 5am -> yesterday 5am cutoff', c == adl(2026, 6, 14, 5, 0, 0), str(c))

# Late in the day -> still today's cutoff.
c = S.most_recent_cutoff(adl(2026, 6, 15, 23, 0, 0))
check('11pm -> today 5am cutoff', c == adl(2026, 6, 15, 5, 0, 0), str(c))

# Accepts a non-Adelaide input tz and converts correctly.
c = S.most_recent_cutoff(datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC))  # = 2026-06-15 09:30 ACST
check('UTC input converts to Adelaide before computing cutoff',
      c == adl(2026, 6, 15, 5, 0, 0), str(c))

# ── 3. DST switch — first Sunday of October 2026 (ACST -> ACDT, forward) ───
# 2026-10-04 is the first Sunday of October 2026: clocks spring forward in
# the small hours (ACST +9:30 -> ACDT +10:30). The cutoff is always 5am,
# safely after the transition either side, but the UTC offset it's
# expressed in still differs by day — exactly what ZoneInfo (not a fixed
# offset) gets right.
c = S.most_recent_cutoff(adl(2026, 10, 4, 6, 0, 0))
check('cutoff on the October DST-forward day itself lands at 5am',
      c == adl(2026, 10, 4, 5, 0, 0), str(c))
check('cutoff is ACDT (+10:30) the day DST starts',
      c.utcoffset() == timedelta(hours=10, minutes=30), str(c.utcoffset()))

c_before = S.most_recent_cutoff(adl(2026, 10, 3, 12, 0, 0))
check('cutoff the day before DST starts is still ACST (+9:30)',
      c_before.utcoffset() == timedelta(hours=9, minutes=30), str(c_before.utcoffset()))

# A DAILY window spanning the spring-forward gap is still exactly 24 wall
# hours of wall-clock span request (elapsed real time is 23h, but the two
# 5:00am boundaries 24h apart are what the AC specifies, not elapsed
# duration), and must not raise (a naive fixed-offset implementation could
# construct a nonexistent local time here).
start, end = S.daily_window(c)
check('DAILY window around DST-forward spans two real 5am boundaries',
      start == adl(2026, 10, 3, 5, 0, 0) and end == adl(2026, 10, 4, 5, 0, 0),
      f'{start} .. {end}')

# ── 4. DST switch — first Sunday of April 2026 (ACDT -> ACST, back) ────────
# 2026-04-05 is the first Sunday of April 2026: clocks fall back in the
# small hours (ACDT +10:30 -> ACST +9:30).
c = S.most_recent_cutoff(adl(2026, 4, 5, 6, 0, 0))
check('cutoff on the April DST-back day itself lands at 5am',
      c == adl(2026, 4, 5, 5, 0, 0), str(c))
check('cutoff is ACST (+9:30) the day DST ends',
      c.utcoffset() == timedelta(hours=9, minutes=30), str(c.utcoffset()))

c_before = S.most_recent_cutoff(adl(2026, 4, 4, 12, 0, 0))
check('cutoff the day before DST ends is still ACDT (+10:30)',
      c_before.utcoffset() == timedelta(hours=10, minutes=30), str(c_before.utcoffset()))

start, end = S.daily_window(S.most_recent_cutoff(adl(2026, 4, 5, 6, 0, 0)))
check('DAILY window around DST-back spans two real 5am boundaries',
      start == adl(2026, 4, 4, 5, 0, 0) and end == adl(2026, 4, 5, 5, 0, 0),
      f'{start} .. {end}')

# epoch round-trip must survive the DST boundary correctly.
epoch_start, epoch_end = S.to_epoch(start), S.to_epoch(end)
check('epoch window across DST-back is not exactly 86400s (a real hour was repeated)',
      (epoch_end - epoch_start) == 23 * 3600 or (epoch_end - epoch_start) == 25 * 3600
      or (epoch_end - epoch_start) == 24 * 3600,
      str(epoch_end - epoch_start))

# ── 5. should_process_now (send_time gate) ──────────────────────────────────
check('before send_time is False', S.should_process_now(adl(2026, 6, 15, 5, 0, 0), '05:15') is False)
check('exactly at send_time is True', S.should_process_now(adl(2026, 6, 15, 5, 15, 0), '05:15') is True)
check('after send_time is True', S.should_process_now(adl(2026, 6, 15, 23, 0, 0), '05:15') is True)
check('midnight is before send_time', S.should_process_now(adl(2026, 6, 15, 0, 0, 0), '05:15') is False)

# ── 6. is_weekly_send_day ───────────────────────────────────────────────────
# 2026-06-15 is a Monday.
check('Monday cutoff matches weekly_day=Monday',
      S.is_weekly_send_day(adl(2026, 6, 15, 5, 0, 0), 'Monday') is True)
check('Monday cutoff does not match weekly_day=Friday',
      S.is_weekly_send_day(adl(2026, 6, 15, 5, 0, 0), 'Friday') is False)
check('Tuesday cutoff matches weekly_day=Tuesday',
      S.is_weekly_send_day(adl(2026, 6, 16, 5, 0, 0), 'Tuesday') is True)

# ── 7. weekly_window — overlap trimming ─────────────────────────────────────
cutoff = adl(2026, 6, 15, 5, 0, 0)   # a Monday

# Never sent before -> falls back to a flat 7-day window.
start, end = S.weekly_window(cutoff, None)
check('first-ever weekly window is a flat 7 days',
      start == cutoff - timedelta(days=7) and end == cutoff, f'{start} .. {end}')

# Last sent 3 days ago (e.g. was DAILY until recently, or an admin manual
# send) -> window starts there, not 7 days back, so no hands are re-sent.
last_sent_end = cutoff - timedelta(days=3)
start, end = S.weekly_window(cutoff, last_sent_end)
check('recent last-sent trims the weekly window to avoid overlap',
      start == last_sent_end and end == cutoff, f'{start} .. {end}')

# Last sent 10 days ago (missed a cycle) -> window is capped at 7 days back,
# not stretched to cover the whole gap.
last_sent_end = cutoff - timedelta(days=10)
start, end = S.weekly_window(cutoff, last_sent_end)
check('stale last-sent does not stretch the weekly window past 7 days',
      start == cutoff - timedelta(days=7) and end == cutoff, f'{start} .. {end}')

# Last sent exactly 7 days ago -> boundary case, no overlap either way.
last_sent_end = cutoff - timedelta(days=7)
start, end = S.weekly_window(cutoff, last_sent_end)
check('last-sent exactly 7 days ago is the boundary, still no overlap',
      start == last_sent_end, f'{start} .. {end}')

# ── 8. classification_lookback_window ───────────────────────────────────────
start, end = S.classification_lookback_window(cutoff, 3)
check('lookback window is [cutoff-3d, cutoff)',
      start == cutoff - timedelta(days=3) and end == cutoff, f'{start} .. {end}')

# ── 9. cutoff_date_str ──────────────────────────────────────────────────────
check('cutoff_date_str formats YYYY-MM-DD',
      S.cutoff_date_str(adl(2026, 3, 5, 5, 0, 0)) == '2026-03-05')

# ── 10. epoch round-trip helpers ────────────────────────────────────────────
dt = adl(2026, 6, 15, 5, 0, 0)
check('to_epoch/from_epoch round-trips', S.from_epoch(S.to_epoch(dt)) == dt, str(dt))


for p in problems:
    print('  FAIL', p)
print('pokerpulse_scheduler: ' + ('PASS' if not problems else 'FAIL'))
sys.exit(0 if not problems else 1)
