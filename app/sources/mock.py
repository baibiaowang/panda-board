"""Deterministic offline data for integration tests. No real market data."""
import hashlib
import math
import random
from datetime import date,timedelta
from types import SimpleNamespace
from .base import AnnouncementSource
from .codes import board_of
from .types import RawAnnouncement,RawKline,DayResult
from ..timeutil import today_cn
from ..window import date_range

_TITLES=['关于重大资产重组的进展公告','关于控股股东股份质押的公告',
         '关于收到中国证监会立案告知书的公告','关于公司股票可能被终止上市的公告',
         '关于回购公司股份方案的公告','关于年度业绩预告的公告',
         '关于修订公司章程的公告','关于董事长辞职的公告']


class MockSource(AnnouncementSource):
    name='mock'

    def __init__(self,seed=20260828,pool_size=800,ann_per_day=150,kline_days=160):
        if not 1<=pool_size<=999 or ann_per_day<0:
            raise ValueError('mock 配置超出范围')
        self.seed=seed
        self.ann_per_day=ann_per_day
        self.kline_days=kline_days
        self.market=SimpleNamespace(kline_errors={},kline_empty=set())
        rng=random.Random(seed)
        self._pool=[]
        for i in range(pool_size):
            prefix=rng.choice(['600','000','300','688','830'])
            code=prefix+f'{i:03d}'
            self._pool.append({'code':code,'name':('ST' if rng.random()<0.05 else '')+f'模拟股{i:04d}',
                'market_value':round(rng.uniform(8,900),2)})

    def _daily(self,day):
        rng=random.Random(f'{self.seed}|{day}')
        records={}
        for _ in range(self.ann_per_day):
            stock=rng.choice(self._pool)
            title=rng.choice(_TITLES)
            aid=hashlib.sha256(f"mock|{stock['code']}|{title}|{day}".encode()).hexdigest()[:24]
            records[aid]=RawAnnouncement(aid,stock['code'],stock['name'],title,day,board_of(stock['code']),
                stock['market_value'],f'https://example.invalid/{aid}')
        return list(records.values())

    def fetch_days(self,start,end):
        for day in date_range(start,end):
            rows=self._daily(day)
            yield DayResult(day,self.name,rows,len(rows),len(rows),True)

    def klines(self,code,start='',end=''):
        end_d=date.fromisoformat(end) if end else today_cn()
        begin=end_d-timedelta(days=self.kline_days*2+15)
        rows=[]
        base=5+int(hashlib.sha256(code.encode()).hexdigest()[:5],16)%7000/100
        previous=None
        for day in date_range(begin.isoformat(),end_d.isoformat()):
            d=date.fromisoformat(day)
            if d.weekday()>=5: continue
            offset=(d-date(2020,1,1)).days
            close=round(base*(1+0.08*math.sin(offset/31)),2)
            opening=round(close*0.998,2)
            high=round(max(opening,close)*1.01,2)
            low=round(min(opening,close)*0.99,2)
            rows.append(RawKline(code,day,opening,high,low,close,123456.0,
                round((close/previous-1)*100,2) if previous else None,'mock','none'))
            previous=close
        return [r for r in rows[-self.kline_days:] if not start or r.date>=start]

    def klines_batch(self,codes,start,end):
        for code in dict.fromkeys(codes):
            yield code,self.klines(code,start,end)

    def probe_day_counts(self,days):
        return {d:len(self._daily(d)) for d in days}

    def close(self):
        pass
