"""Eastmoney announcements: verify unique source documents before stock expansion."""
from __future__ import annotations
import hashlib
import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from ..config import get_fetch_config
from ..window import date_range
from .base import AnnouncementSource
from .codes import valid_code, board_of
from .types import RawAnnouncement, DayResult, SourceError

ANN_HOSTS=('https://np-anotice-stock.eastmoney.com',)
ANN_PATH='/api/security/ann'
DETAIL_URL='https://data.eastmoney.com/notices/detail/{code}/{art_code}.html'


class EastmoneySource(AnnouncementSource):
    name='eastmoney'

    def __init__(self, market=None):
        super().__init__(market)
        cfg=get_fetch_config().get('eastmoney',{})
        self.page_size=max(1,min(100,int(cfg.get('page_size',100))))
        self.max_pages=max(1,int(cfg.get('max_pages_per_day',120)))
        self.ann_type=str(cfg.get('ann_type','A'))
        self.page_sleep=max(0,float(cfg.get('sleep',0.35)))
        self.day_workers=max(1,min(8,int(cfg.get('day_workers',4))))
        self.probe_workers=max(1,min(8,int(cfg.get('probe_workers',4))))

    def _query(self, host, day, page):
        qs=urllib.parse.urlencode({'sr':-1,'page_size':self.page_size,'page_index':page,
            'ann_type':self.ann_type,'client_source':'web','begin_time':day,'end_time':day,'f_node':0,'s_node':0})
        response=self.market.http.get_json(host+ANN_PATH+'?'+qs,{'Referer':'https://data.eastmoney.com/'})
        data=response.get('data') if isinstance(response,dict) else None
        if (not isinstance(data,dict) or 'total_hits' not in data or
                not isinstance(data.get('list'),list) or response.get('success',1) not in (1,True)):
            raise SourceError('东方财富返回了无效响应结构或业务错误')
        total=int(data['total_hits'])
        if total<0:
            raise SourceError('东方财富 total_hits 无效')
        return response

    @staticmethod
    def _document_key(item):
        if not isinstance(item,dict):
            raise SourceError('东方财富公告记录不是对象')
        return str(item.get('art_code') or hashlib.sha256(json.dumps(item,sort_keys=True,ensure_ascii=False).encode()).hexdigest())

    def _fetch_day(self, day):
        result=DayResult(day,self.name)
        docs={}
        try:
            first=self._query(ANN_HOSTS[0],day,1)['data']
            total=int(first['total_hits'])
            result.expected=total
            pages=max(1,(total+self.page_size-1)//self.page_size)
            errors=[]
            if pages>self.max_pages:
                errors.append(f'超过分页上限: 需要{pages}页，配置{self.max_pages}页')
            for p in range(1,min(pages,self.max_pages)+1):
                try:
                    data=first if p==1 else self._query(ANN_HOSTS[0],day,p)['data']
                    if int(data['total_hits'])!=total:
                        errors.append('翻页期间源总数变化，需要重抓')
                    if not data['list'] and total>0:
                        errors.append(f'第{p}页意外为空')
                        break
                    for item in data['list']:
                        docs[self._document_key(item)]=item
                except Exception as exc:
                    errors.append(f'第{p}页失败: {exc}')
                    break
                if p<min(pages,self.max_pages) and self.page_sleep:
                    time.sleep(self.page_sleep)
            if len(docs)!=total:
                errors.append(f'唯一原始公告数 {len(docs)}/{total}')
            for key,item in docs.items():
                try:
                    title=str(item.get('title_ch') or item.get('title') or '').strip()
                    dt=str(item.get('notice_date') or item.get('display_time') or '')[:10]
                    codes=item.get('codes')
                    if not isinstance(codes,list):
                        raise SourceError('缺少关联证券列表')
                    for security in codes:
                        code=str(security.get('stock_code') or '').strip()
                        if not valid_code(code):
                            continue
                        if not title or dt!=day or date.fromisoformat(dt).isoformat()!=dt:
                            raise SourceError('公告标题为空或公告日期越界')
                        art=str(item.get('art_code') or '').strip()
                        aid=f'{art}#{code}' if art else f'em:{key}#{code}'
                        result.announcements.append(RawAnnouncement(aid,code,str(security.get('short_name') or '').strip(),
                            title,dt,board_of(code),url=DETAIL_URL.format(code=code,art_code=urllib.parse.quote(art,safe='')) if art else ''))
                except Exception as exc:
                    errors.append(f'公告解析失败: {exc}')
            result.complete=not errors
            result.error='; '.join(dict.fromkeys(errors))[:1000]
        except Exception as exc:
            result.error=str(exc)[:1000]
        result.fetched=len(docs)
        return result

    def fetch_days(self,start,end):
        yield from self.fetch_dates(date_range(start,end))

    def fetch_dates(self,days):
        with ThreadPoolExecutor(max_workers=self.day_workers) as ex:
            for result in ex.map(self._fetch_day,days):
                codes={a.code for a in result.announcements}
                self.prefetch_market_caps(codes)
                for ann in result.announcements:
                    ann.market_value=self.market_cap(ann.code)
                yield result

    def probe_day_counts(self,days):
        def work(day):
            try:
                return day,int(self._query(ANN_HOSTS[0],day,1)['data']['total_hits'])
            except Exception:
                return day,None
        with ThreadPoolExecutor(max_workers=self.probe_workers) as ex:
            return {d:n for d,n in ex.map(work,list(days)) if n is not None}

    def max_items_per_day(self):
        return self.page_size*self.max_pages
