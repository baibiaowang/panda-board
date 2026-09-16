"""Validated daily price snapshots; transient errors never become 'no data'."""
from __future__ import annotations
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import date,timedelta
from urllib.parse import urlencode
from ..config import get_fetch_config
from ..paths import cache_path
from ..timeutil import today_cn
from .codes import secid,tx_symbol,valid_code
from .http import HttpClient
from .types import RawKline,SourceError

EASTMONEY_KLINE='https://push2his.eastmoney.com/api/qt/stock/kline/get'
TENCENT_KLINE='https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
TENCENT_QUOTE='https://qt.gtimg.cn/q='
NODATA_FILE='kline_nodata_v2.json'
MV_CACHE_FILE='market_cap_cache.json'


class MarketData:
    def __init__(self,cfg=None,http=None):
        fetch=cfg if cfg is not None else get_fetch_config()
        kline=fetch.get('kline',{})
        mv=fetch.get('market_cap',{})
        self.http=http or HttpClient()
        self.kline_days=max(6,min(640,int(kline.get('days',160))))
        self.kline_workers=max(1,min(16,int(kline.get('workers',8))))
        self.eastmoney_first=bool(kline.get('eastmoney_first',False))
        self.nodata_retry_days=max(0,min(7,int(kline.get('nodata_retry_days',1))))
        self.mv_enabled=bool(mv.get('enabled',True))
        self.mv_batch=max(1,min(100,int(mv.get('batch_size',60))))
        self.mv_workers=max(1,min(8,int(mv.get('workers',4))))
        self.mv_cache_days=float(mv.get('cache_days',0))
        self._nodata_path=cache_path(NODATA_FILE)
        self._mv_path=cache_path(MV_CACHE_FILE)
        self._nodata=_load_json(self._nodata_path)
        self._mv=_load_json(self._mv_path)
        self._lock=threading.RLock()
        self._mv_attempted=set()
        self.kline_errors={}
        self.kline_empty=set()
        self.market_cap_errors={}
        self._nodata_dirty=False
        self._mv_dirty=False

    def _cache_key(self,code,start,end):
        return f'{code}|{start}|{end}|{self.eastmoney_first}|qfq'

    def _nodata_recent(self,code,start='',end=''):
        if self.nodata_retry_days<=0:
            return False
        with self._lock:
            stamp=self._nodata.get(self._cache_key(code,start,end))
        try:
            age=time.time()-float(stamp)
            return 0<=age<self.nodata_retry_days*86400
        except (ValueError,TypeError):
            return False

    def _fetch_rows(self,code,start='',end=''):
        """Return raw rows, provider, adjustment. Require a structurally valid response."""
        providers=['eastmoney','tencent'] if self.eastmoney_first else ['tencent','eastmoney']
        errors=[]
        for provider in providers:
            try:
                if provider=='eastmoney':
                    query={'secid':secid(code),'fields1':'f1,f2,f3,f4,f5,f6',
                        'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59','klt':101,'fqt':1,
                        'end':end.replace('-','') if end else '20500101','lmt':self.kline_days+1}
                    if start: query['beg']=(date.fromisoformat(start)-timedelta(days=15)).strftime('%Y%m%d')
                    reply=self.http.get_json(EASTMONEY_KLINE+'?'+urlencode(query),{'Referer':'https://quote.eastmoney.com/'})
                    data=reply.get('data') if isinstance(reply,dict) else None
                    if not isinstance(data,dict) or not isinstance(data.get('klines'),list) or reply.get('rc',0)!=0:
                        raise SourceError('东财K线响应结构或业务状态无效')
                    returned=str(data.get('code') or '')
                    if returned!=code: raise SourceError('东财返回的股票代码不匹配')
                    rows=[r.split(',') for r in data['klines']]
                    adjustment='qfq'
                else:
                    symbol=tx_symbol(code)
                    begin=(date.fromisoformat(start)-timedelta(days=15)).isoformat() if start else ''
                    query={'param':f'{symbol},day,{begin},{end},{self.kline_days+1},qfq'}
                    reply=self.http.get_json(TENCENT_KLINE+'?'+urlencode(query),{'Referer':'https://gu.qq.com/'})
                    if not isinstance(reply,dict) or reply.get('code',0)!=0:
                        raise SourceError('腾讯K线业务状态无效')
                    data=(reply.get('data') or {}).get(symbol)
                    if not isinstance(data,dict): raise SourceError('腾讯K线缺少对应股票的数据对象')
                    if isinstance(data.get('qfqday'),list):
                        rows=data['qfqday']; adjustment='qfq'
                    elif isinstance(data.get('day'),list):
                        rows=data['day']; adjustment='none'
                    else:
                        raise SourceError('腾讯K线缺少行情数组')
                if rows:
                    # Validate here so malformed primary data can still use a valid fallback.
                    parsed=self._parse_rows(code,rows,provider,adjustment)
                    if not any((not start or k.date>=start) and (not end or k.date<=end) for k in parsed):
                        raise SourceError('行情源忽略请求日期，未返回区间内数据')
                    return parsed,False
            except Exception as exc:
                errors.append(f'{provider}: {exc}')
        if errors:
            raise SourceError('; '.join(errors))
        return [],True

    @staticmethod
    def _parse_rows(code,rows,provider,adjustment):
        parsed={}
        for row in rows:
            if not isinstance(row,(list,tuple)) or len(row)<6:
                raise SourceError('K线字段不足')
            d=str(row[0])
            if date.fromisoformat(d).isoformat()!=d:
                raise SourceError('K线日期格式无效')
            o,c,h,l,v=map(float,row[1:6])
            if any(not math.isfinite(x) for x in (o,c,h,l,v)) or min(o,c,h,l)<=0 or v<0 or h<max(o,c) or l>min(o,c):
                raise SourceError('K线 OHLC 或成交量无效')
            bar=RawKline(code,d,o,h,l,c,v,None,provider,adjustment)
            if d in parsed and parsed[d]!=bar:
                raise SourceError('同日K线存在冲突记录')
            parsed[d]=bar
        result=[parsed[d] for d in sorted(parsed)]
        previous=None
        for bar in result:
            bar.change_pct=round((bar.close/previous-1)*100,2) if previous else None
            previous=bar.close
        return result

    def klines(self,code,start='',end=''):
        if not valid_code(code): raise ValueError('无效股票代码')
        if start: date.fromisoformat(start)
        if end: date.fromisoformat(end)
        if start and end and start>end: raise ValueError('K线日期区间倒置')
        if self._nodata_recent(code,start,end):
            with self._lock: self.kline_empty.add(code)
            return []
        rows,empty=self._fetch_rows(code,start,end)
        if empty:
            with self._lock:
                self._nodata[self._cache_key(code,start,end)]=time.time()
                self._nodata_dirty=True
                self.kline_empty.add(code)
            return []
        out=[k for k in rows if (not start or k.date>=start) and (not end or k.date<=end)]
        if not out:
            # Data outside a requested historical window is not global absence.
            raise SourceError('行情源未返回请求区间内的K线')
        return out[-self.kline_days:]

    def klines_batch(self,codes,start,end):
        codes=list(dict.fromkeys(codes))
        self.kline_errors={}
        self.kline_empty=set()
        def work(code):
            try:
                return code,self.klines(code,start,end)
            except Exception as exc:
                with self._lock: self.kline_errors[code]=str(exc)
                return code,[]
        try:
            with ThreadPoolExecutor(max_workers=self.kline_workers) as ex:
                for future in as_completed([ex.submit(work,c) for c in codes]):
                    yield future.result()
        finally:
            self.save()

    def _mv_fresh(self,code):
        hit=self._mv.get(code)
        try:
            value=float(hit['v'])
            age=time.time()-float(hit['t'])
            return math.isfinite(value) and value>0 and (self.mv_cache_days<=0 or 0<=age<self.mv_cache_days*86400)
        except (TypeError,ValueError,KeyError):
            return False

    @staticmethod
    def _parse_quote(raw):
        out={}
        for segment in raw.split(';'):
            key,sep,val=segment.strip().partition('=')
            parts=val.strip().strip('"').split('~')
            if not sep or not key.startswith('v_') or len(parts)<=45: continue
            try: value=float(parts[45])
            except (ValueError,TypeError): continue
            if math.isfinite(value) and value>0: out[key[2:]]=value
        return out

    def market_caps(self,codes):
        if not self.mv_enabled: return {}
        codes=[c for c in dict.fromkeys(codes) if valid_code(c)]
        todo=[c for c in codes if not self._mv_fresh(c) and c not in self._mv_attempted]
        self._mv_attempted.update(todo)
        def work(chunk):
            mapping={tx_symbol(c):c for c in chunk}
            try:
                raw=self.http.get(TENCENT_QUOTE+','.join(mapping),encoding='gbk')
                got=self._parse_quote(raw)
                return {mapping[s]:v for s,v in got.items() if s in mapping}
            except Exception as exc:
                with self._lock:
                    for c in chunk: self.market_cap_errors[c]=type(exc).__name__
                return {}
        chunks=[todo[i:i+self.mv_batch] for i in range(0,len(todo),self.mv_batch)]
        with ThreadPoolExecutor(max_workers=self.mv_workers) as ex:
            for result in ex.map(work,chunks):
                for code,value in result.items():
                    self._mv[code]={'v':value,'t':time.time()}
                    self._mv_dirty=True
        out={}
        for code in codes:
            try:
                value=float(self._mv[code]['v'])
                if math.isfinite(value) and value>0: out[code]=value
            except (KeyError,TypeError,ValueError): pass
        return out

    def prefetch_market_caps(self,codes):
        return len(self.market_caps(codes))

    def market_cap(self,code):
        if not self.mv_enabled:
            return 0.0
        if self._mv_fresh(code) or code in self._mv_attempted:
            try:
                value=float(self._mv.get(code,{}).get('v',0))
                return value if math.isfinite(value) and value>0 else 0.0
            except (ValueError,TypeError):
                return 0.0
        return self.market_caps([code]).get(code,0.0)

    def save(self):
        with self._lock:
            if self._nodata_dirty:
                # Old range-specific cache entries have no future value.
                self._nodata={k:v for k,v in self._nodata.items() if isinstance(v,(int,float)) and 0<=time.time()-v<8*86400}
                _save_json(self._nodata_path,self._nodata)
                self._nodata_dirty=False
            if self._mv_dirty:
                _save_json(self._mv_path,self._mv)
                self._mv_dirty=False

    def close(self):
        try:
            self.save()
        finally:
            self.http.close()


def _load_json(path):
    try:
        with open(path,encoding='utf-8') as f: result=json.load(f)
        return result if isinstance(result,dict) else {}
    except (OSError,ValueError): return {}


def _save_json(path,data):
    os.makedirs(os.path.dirname(path),exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.cache-',dir=os.path.dirname(path))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
