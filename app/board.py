"""Board payloads include provenance, coverage, and actual price dates."""
from __future__ import annotations
import json
from datetime import date,timedelta
from . import db,store
from .config import get_display_config
from .sources import source_name
from .taxonomy import get_engine
from .timeutil import today_cn,now_cn
from .window import date_range


def window_dates(days):
    if days<1: raise ValueError('展示天数必须为正')
    end=today_cn()
    return (end-timedelta(days=days-1)).isoformat(),end.isoformat()


def _chunks(values,n=400):
    for i in range(0,len(values),n): yield values[i:i+n]


def _chg_stats(codes,min_ann_dates):
    out={}
    for chunk in _chunks(codes):
        marks=','.join('?' for _ in chunk)
        rows=db.query(f'''SELECT * FROM (SELECT code,date,close,source,adjustment,
            ROW_NUMBER() OVER(PARTITION BY code ORDER BY date DESC) rn
            FROM current_klines WHERE code IN ({marks})) WHERE rn<=6 ORDER BY code,rn''',chunk)
        prices={}
        for row in rows: prices.setdefault(row['code'],[]).append(row)
        for code,bars in prices.items():
            last=bars[0]['close']
            def change(n):
                return round((last/bars[n]['close']-1)*100,2) if len(bars)>n and bars[n]['close'] else None
            out[code]={'last_close':last,'chg':change(1),'chg5':change(5),'chg_ann':None,
                'price_date':bars[0]['date'],'price_source':bars[0]['source'],'adjustment':bars[0]['adjustment']}
    todo=[(code,min_ann_dates[code]) for code in codes if code in out and min_ann_dates.get(code)]
    for chunk in _chunks(todo):
        sql='''WITH anchors(code,min_date) AS (VALUES %s)
            SELECT a.code,
            (SELECT date FROM current_klines k WHERE k.code=a.code AND k.date>=a.min_date ORDER BY date LIMIT 1) anchor,
            (SELECT close FROM current_klines k WHERE k.code=a.code AND k.date<a.min_date ORDER BY date DESC LIMIT 1) base
            FROM anchors a''' % ','.join('(?,?)' for _ in chunk)
        for row in db.query(sql,[x for pair in chunk for x in pair]):
            # No previous close means the return is unknown, never substitute an unrelated open.
            if row['anchor'] and row['base']:
                out[row['code']]['chg_ann']=round((out[row['code']]['last_close']/row['base']-1)*100,2)
    return out


def detect_gaps(days=90,thin_below=5):
    start,end=window_dates(days)
    calendar=date_range(start,end)
    source=source_name()
    records={r['date']:dict(r) for r in db.query('SELECT * FROM fetch_days_v2 WHERE source=? AND date BETWEEN ? AND ?',
        (source,start,end))}
    unknown=[d for d in calendar if d not in records]
    incomplete=[d for d in calendar if d in records and not records[d]['complete']]
    complete=[d for d in calendar if d in records and records[d]['complete']]
    zeros=[d for d in complete if records[d]['expected']==0]
    return {'window':{'start':start,'end':end,'days':days},'source':source,'calendar_source':'calendar_days',
        'covered_days':len(complete),'missing_days':unknown,'incomplete_days':incomplete,'confirmed_empty_days':zeros,
        'coverage':round(len(complete)/len(calendar)*100,1),'thin_days':[],
        'verdict':'ok' if not unknown and not incomplete else '待补抓或核验',
        'note':'完整表示所选来源逐日分页计数通过；不等于交易所全覆盖或已验证公告全文。'}


def build_payload(days=90):
    db.init_db()
    engine=get_engine()
    # 展示窗口必须与判定层同一口径：取 min(配置窗口, 已采集跨度)。
    # 否则判定层放行、展示层却报"缺 N 天"，自己打自己脸（跨度不足 90 天时必现）。
    src=source_name()
    earliest=db.scalar('SELECT MIN(date) FROM fetch_days_v2 WHERE source=?',(src,))
    if earliest:
        days=max(1,min(days,(today_cn()-date.fromisoformat(earliest)).days+1))
    start,end=window_dates(days)
    rows=db.query("SELECT * FROM announcements WHERE category<>'other' AND is_noise=0 AND date BETWEEN ? AND ? ORDER BY code,date,ann_id",(start,end))
    agg={}
    seen=set()
    for row in rows:
        key=(row['code'],row['date'],row['title'])
        if key in seen: continue
        seen.add(key)
        entry=agg.setdefault(row['code'],{'code':row['code'],'name':'','board':row['board'],'anns':[]})
        if row['name']: entry['name']=row['name']
        entry['anns'].append({'date':row['date'],'title':row['title'],'url':row['url'] or '',
            'category':engine.label_of(row['category']),'category_id':row['category'],'source':row['source']})
    codes=list(agg)
    # 市值取最新一天有数据的快照。拿不到就留空，不拿旧的缓存值冒充当天数据。
    values=store.market_caps_for(codes)
    stats=_chg_stats(codes,{c:agg[c]['anns'][0]['date'] for c in codes})
    items=[]
    display=get_display_config()
    for code,entry in agg.items():
        s=stats.get(code,{})
        chosen=entry['anns'][0 if display.get('category_source')=='earliest' else -1]
        items.append({'code':code,'name':entry['name'],'board':entry['board'],'is_st':'ST' in entry['name'].upper(),
            'category':chosen['category'],'category_id':chosen['category_id'],
            'market_cap':round(float(values.get(code,0))*1e8,2),'announcements':entry['anns'],
            'last_close':s.get('last_close'),'chg':s.get('chg'),'chg5':s.get('chg5'),'chg_ann':s.get('chg_ann'),
            'price_date':s.get('price_date'),'price_source':s.get('price_source'),'adjustment':s.get('adjustment')})
    items.sort(key=lambda i:(i['announcements'][-1]['date'],i['code']),reverse=True)
    return {'range':{'start':start,'end':end},'items':items,'taxonomy':engine.taxonomy(),
        'meta':{'generated_at':now_cn().isoformat(),'source':source_name(),'mock':source_name()=='mock',
                'latest_announcement':db.scalar('SELECT MAX(date) FROM announcements'),
                'latest_price':db.scalar('SELECT MAX(date) FROM current_klines'),
                'run':store.latest_run(),'coverage':detect_gaps(days),
                'detail_limit':int(display.get('max_announcements_per_stock',0))}}


def render_js(payload):
    compact=lambda value:json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
    return ('window.ANNO_LIST = '+compact(payload['items'])+';\n'
            'window.ANNO_META = '+compact(payload.get('meta',{}))+';\n'
            'window.ANNO_TAXONOMY = '+compact(payload.get('taxonomy',[]))+';\n')
