"""A-share business dates are always Asia/Shanghai, independent of server TZ.

UTC 16:00–24:00 is already the next calendar day in China. Timestamps stored in
the legacy SQLite columns remain naive Beijing time; exported values carry +08:00.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FALLBACK_TZ = timezone(timedelta(hours=8), name='UTC+8')
try:
    _CN_TZ = ZoneInfo('Asia/Shanghai')
except ZoneInfoNotFoundError:
    _CN_TZ = FALLBACK_TZ


def tz_name():
    return 'Asia/Shanghai'


def cn_tz():
    return _CN_TZ


def now_cn():
    return datetime.now(_CN_TZ)


def today_cn():
    return now_cn().date()


def now_naive():
    return now_cn().replace(tzinfo=None)


def iso_cn(dt):
    if dt is None:
        return None
    return (dt.replace(tzinfo=_CN_TZ) if dt.tzinfo is None else dt.astimezone(_CN_TZ)).isoformat()


def weekday_range(start: date, end: date):
    """Compatibility helper only; NOT an exchange calendar or announcement coverage test."""
    return [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)
            if (start+timedelta(days=i)).weekday() < 5]


def tz_report():
    import time
    n = now_cn()
    return {'configured': tz_name(), 'resolved': str(_CN_TZ),
            'utc_offset': n.utcoffset().total_seconds()/3600,
            'now_cn': n.isoformat(), 'today_cn': n.date().isoformat(),
            'system_tz': time.tzname[0],
            'server_tz_mismatch': datetime.now().date() != n.date(),
            'tzdata_ok': _CN_TZ is not FALLBACK_TZ}
