"""Atomic day ingestion, upserts, coherent adjusted-price snapshots and run history."""
from __future__ import annotations
import hashlib
import json
import math
from datetime import date, datetime
from . import db
from .sources.codes import valid_code
from .timeutil import now_naive, iso_cn

ANN_COLUMNS = ('ann_id','code','name','title','date','category','board','key_numbers','url','summary','created_at','source','is_noise')
KLINE_COLUMNS = ('code','date','open','high','low','close','volume','change_pct')


def _valid_date(value):
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('日期必须使用 YYYY-MM-DD')


def save_announcements(rows):
    rows = list(rows)
    if not rows:
        return 0
    normalized = []
    for r in rows:
        r = tuple(r)
        if len(r) == 11:
            r += ('legacy', 0)
        if not valid_code(r[1]) or not r[0] or not r[4] or not r[3]:
            raise ValueError('公告缺少有效编号、股票代码、日期或标题')
        _valid_date(r[4])
        normalized.append(r)
    updates = ','.join(f'{c}=excluded.{c}' for c in ANN_COLUMNS[1:] if c != 'created_at')
    changes = ' OR '.join(f'{c} IS NOT excluded.{c}' for c in ANN_COLUMNS[1:] if c != 'created_at')
    with db.transaction() as c:
        before = c.total_changes
        c.executemany(f"INSERT INTO announcements ({','.join(ANN_COLUMNS)}) VALUES ({','.join('?' for _ in ANN_COLUMNS)}) "
                      f'ON CONFLICT(ann_id) DO UPDATE SET {updates} WHERE {changes}', normalized)
        return c.total_changes - before


def save_day(result, engine):
    """Do not delete prior records, even if a later fetch is incomplete or empty."""
    _valid_date(result.date)
    if result.fetched < 0 or (result.expected is not None and result.expected < 0):
        raise ValueError('公告分页计数不能为负')
    if result.complete and (result.expected is None or result.fetched != result.expected or result.error):
        raise ValueError('完整日必须有相等的源计数且没有错误')
    stamp = now_naive()
    records = {}
    stocks = {}
    noise = 0
    for raw in result.announcements:
        if raw.date != result.date:
            raise ValueError(f'公告日期越界: {raw.date} != {result.date}')
        category = engine.classify(raw.title, raw.summary)
        is_noise = int(engine.is_noise(raw.title))
        noise += is_noise
        records[raw.ann_id] = (raw.ann_id,raw.code,raw.name,raw.title,raw.date,category,
            raw.board,','.join(engine.extract_numbers(raw.title+' '+raw.summary)),raw.url,
            raw.summary,stamp,result.source,is_noise)
        stocks[raw.code] = (raw.code,raw.name,raw.board,raw.market_value,stamp)
    with db.transaction() as c:
        new = 0
        for aid in records:
            if c.execute('SELECT 1 FROM announcements WHERE ann_id=?',(aid,)).fetchone() is None:
                new += 1
        changed = save_announcements(records.values())
        save_stocks(stocks.values())
        kept = sum(1 for r in records.values() if not r[-1])
        c.execute('''INSERT INTO fetch_days_v2(date,source,fetched,expected,kept,complete,updated_at,error)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(date,source) DO UPDATE SET
            fetched=excluded.fetched,expected=excluded.expected,kept=excluded.kept,
            complete=excluded.complete,updated_at=excluded.updated_at,error=excluded.error''',
            (result.date,result.source,result.fetched,result.expected,kept,int(result.complete),stamp,result.error[:1000]))
        # Maintain the old table for existing read-only dashboards/tools. Never use it as v2 evidence.
        c.execute('''INSERT INTO day_fetch(date,source,fetched,kept,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET source=excluded.source,fetched=excluded.fetched,
            kept=excluded.kept,updated_at=excluded.updated_at''',
            (result.date,result.source,result.fetched,kept,stamp))
    return {'new':new,'changed':changed,'noise':noise,'stocks':len(stocks)}


def save_stocks(rows):
    with db.transaction() as c:
        before = c.total_changes
        c.executemany('''INSERT INTO stocks(code,name,board,market_value,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
            name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE stocks.name END,
            board=CASE WHEN excluded.board<>'' THEN excluded.board ELSE stocks.board END,
            market_value=CASE WHEN excluded.market_value>0 THEN excluded.market_value ELSE stocks.market_value END,
            updated_at=excluded.updated_at''',rows)
        return c.total_changes-before


def validate_klines(rows):
    seen=set()
    for r in rows:
        if not valid_code(r[0]):
            raise ValueError('K线股票代码无效')
        _valid_date(r[1])
        if (r[0],r[1]) in seen:
            raise ValueError('K线日期重复')
        seen.add((r[0],r[1]))
        o,h,l,close,vol=r[2:7]
        if any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in (o,h,l,close,vol)):
            raise ValueError('K线包含非有限数值')
        if min(o,h,l,close)<=0 or vol<0 or l>min(o,close) or h<max(o,close) or l>h:
            raise ValueError('K线 OHLC/成交量关系无效')
        if r[7] is not None and not math.isfinite(r[7]):
            raise ValueError('K线涨跌幅无效')


def save_klines(rows):
    """Compatibility upsert. Pipeline uses save_kline_snapshot for coherent qfq data."""
    rows=list(rows)
    validate_klines(rows)
    with db.transaction() as c:
        for code in {r[0] for r in rows}:
            if c.execute('SELECT 1 FROM kline_sync WHERE code=?',(code,)).fetchone():
                raise ValueError('已迁入快照的股票只能使用 save_kline_snapshot 更新完整价格段')
        before=c.total_changes
        c.executemany('''INSERT INTO klines(code,date,open,high,low,close,volume,change_pct)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET
            open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
            volume=excluded.volume,change_pct=excluded.change_pct
            WHERE open IS NOT excluded.open OR high IS NOT excluded.high OR low IS NOT excluded.low
            OR close IS NOT excluded.close OR volume IS NOT excluded.volume OR change_pct IS NOT excluded.change_pct''',rows)
        return c.total_changes-before


def save_kline_snapshot(code, bars, target_end):
    if not bars:
        return 0
    if any(k.code!=code for k in bars):
        raise ValueError('行情返回了其他股票的数据')
    providers={(k.source,k.adjustment) for k in bars}
    if len(providers)!=1:
        raise ValueError('单次快照不能混用行情源或复权口径')
    rows=[(k.code,k.date,k.open,k.high,k.low,k.close,k.volume,k.change_pct) for k in bars]
    validate_klines(rows)
    rows.sort(key=lambda r:r[1])
    if rows[-1][1]>target_end:
        raise ValueError('K线超出目标日期')
    provider,adjustment=next(iter(providers))
    key=hashlib.sha256(json.dumps([provider,adjustment,rows],allow_nan=False,separators=(',',':')).encode()).hexdigest()
    with db.transaction() as c:
        old=c.execute('SELECT snapshot_key FROM kline_sync WHERE code=?',(code,)).fetchone()
        changed=0
        if not old or old[0]!=key:
            before=c.total_changes
            c.executemany('''INSERT INTO klines(code,date,open,high,low,close,volume,change_pct,source,adjustment,snapshot_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                volume=excluded.volume,change_pct=excluded.change_pct,source=excluded.source,
                adjustment=excluded.adjustment,snapshot_key=excluded.snapshot_key''',
                [r+(provider,adjustment,key) for r in rows])
            changed=c.total_changes-before
        c.execute('''INSERT INTO kline_sync(code,source,adjustment,snapshot_key,start_date,end_date,target_end,checked_at,status,error)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET source=excluded.source,
            adjustment=excluded.adjustment,snapshot_key=excluded.snapshot_key,start_date=excluded.start_date,
            end_date=excluded.end_date,target_end=excluded.target_end,checked_at=excluded.checked_at,status='success',error='' ''',
            (code,provider,adjustment,key,rows[0][1],rows[-1][1],target_end,now_naive(),'success',''))
    return changed


def meta_get(key, default=None):
    return db.scalar('SELECT value FROM board_meta WHERE key=?',(key,),default)


def meta_set(key,value):
    with db.transaction() as c:
        c.execute('INSERT INTO board_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))


def start_run(mode):
    with db.transaction() as c:
        return c.execute("INSERT INTO runs(mode,status,started_at,fetched,new_count) VALUES(?,'running',?,0,0)",(mode,now_naive())).lastrowid


def finish_run(run_id, *, fetched, new_count, duration_ms, peak_rss_mb=None, errors=None):
    errors=errors or []
    with db.transaction() as c:
        c.execute('UPDATE runs SET status=?,finished_at=?,fetched=?,new_count=?,duration_ms=?,peak_rss_mb=?,error=? WHERE id=?',
            ('partial' if errors else 'success',now_naive(),fetched,new_count,duration_ms,peak_rss_mb,'; '.join(errors)[-2000:] or None,run_id))


def fail_run(run_id,error,duration_ms):
    with db.transaction() as c:
        c.execute("UPDATE runs SET status='failed',finished_at=?,duration_ms=?,error=? WHERE id=?",(now_naive(),duration_ms,str(error)[-2000:],run_id))


def latest_run():
    row=db.one('SELECT * FROM runs ORDER BY id DESC LIMIT 1')
    if row is None:
        return None
    out=dict(row)
    for key in ('started_at','finished_at'):
        out[key]=iso_cn(datetime.fromisoformat(out[key])) if out[key] else None
    return out


def _chunks(values,n=400):
    values=list(values)
    for i in range(0,len(values),n): yield values[i:i+n]


def save_market_caps(rows):
    """按 (code, date) 记录市值。同一天重复采集只覆盖，不产生重复行。

    零值和无效值不写入：市值拿不到时保持该日空缺，不用 0 冒充已知数据。
    """
    stamp=now_naive()
    normalized=[]
    for code,day,value in rows:
        if not valid_code(code):
            raise ValueError('市值股票代码无效')
        _valid_date(day)
        try:
            number=float(value)
        except (TypeError,ValueError):
            raise ValueError('市值必须是数值')
        if not math.isfinite(number) or number<=0:
            continue
        normalized.append((code,day,number,stamp))
    if not normalized:
        return 0
    with db.transaction() as c:
        before=c.total_changes
        c.executemany('''INSERT INTO market_caps(code,date,market_value,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET
            market_value=excluded.market_value,updated_at=excluded.updated_at
            WHERE market_value IS NOT excluded.market_value''',normalized)
        return c.total_changes-before


def latest_market_cap_date():
    return db.scalar('SELECT MAX(date) FROM market_caps')


def market_caps_for(codes,day=None):
    """取某一天（默认最新有数据的一天）的市值映射。"""
    day=day or latest_market_cap_date()
    if not day: return {}
    out={}
    for chunk in _chunks(codes):
        marks=','.join('?' for _ in chunk)
        for row in db.query('SELECT code,market_value FROM market_caps WHERE date=? AND code IN (%s)'%marks,[day,*chunk]):
            out[row['code']]=row['market_value']
    return out
