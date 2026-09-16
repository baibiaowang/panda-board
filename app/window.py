"""Always fetch the current window, then repair bounded, explicit historical days."""
from datetime import date,timedelta
from . import db
from .timeutil import today_cn,now_naive

SCAN_DAYS=90
MAX_DAYS_PER_RUN=12


def date_range(start_iso,end_iso):
    start,end=date.fromisoformat(start_iso),date.fromisoformat(end_iso)
    if start.isoformat()!=start_iso or end.isoformat()!=end_iso or start>end:
        raise ValueError('日期必须为 YYYY-MM-DD，且开始日期不能晚于结束日期')
    if (end-start).days>3660:
        raise ValueError('一次日期范围不得超过十年')
    return [(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]


def plan_dates(end_iso,lookback_days,source,scan_days=SCAN_DAYS,budget=MAX_DAYS_PER_RUN):
    if lookback_days<1 or scan_days<1 or budget<lookback_days:
        raise ValueError('回看、扫描天数必须为正，单轮预算不能小于回看天数')
    end=date.fromisoformat(end_iso)
    recent=date_range((end-timedelta(days=lookback_days-1)).isoformat(),end_iso)
    scan=date_range((end-timedelta(days=scan_days-1)).isoformat(),end_iso)
    records={r['date']:dict(r) for r in db.query('SELECT * FROM fetch_days_v2 WHERE source=? AND date BETWEEN ? AND ?',
        (source,scan[0],end_iso))}
    stale_before=(now_naive()-timedelta(days=7)).isoformat(sep=' ')
    todo=[]
    for day in scan:
        if day in recent: continue
        record=records.get(day)
        if not record or not record['complete'] or record['updated_at']<stale_before:
            # Oldest attempted day first; unverified dates first, newest unverified date first.
            todo.append((record['updated_at'] if record else '',-date.fromisoformat(day).toordinal(),day))
    todo.sort()
    # Consume today's window first, not merely include it at the end of a long backfill.
    return recent+[r[2] for r in todo[:budget-len(recent)]]


def resolve_incremental(end_iso,lookback_days,probe=None,day_cap=0,fetched=None):
    """Legacy caller compatibility. Disjoint backfill is now planned by plan_dates()."""
    if lookback_days<1: raise ValueError('lookback_days 必须为正')
    start=(date.fromisoformat(end_iso)-timedelta(days=lookback_days-1)).isoformat()
    return start,end_iso,f'常规回看 {lookback_days} 天；历史补漏由独立日期队列处理',[]


def kline_cover_cutoff(end_iso,slack_days=0):
    return (date.fromisoformat(end_iso)-timedelta(days=slack_days)).isoformat()
