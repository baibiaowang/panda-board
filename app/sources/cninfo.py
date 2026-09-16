"""Cninfo daily queries. A failed exchange/page is never a successful empty day."""
from __future__ import annotations
import hashlib
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit
from ..config import get_fetch_config
from ..timeutil import cn_tz
from ..window import date_range
from .base import AnnouncementSource
from .codes import valid_code,board_of
from .types import RawAnnouncement,DayResult,SourceError

CNINFO_URL='https://www.cninfo.com.cn/new/hisAnnouncement/query'
PAGE_SIZE=30


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(int(ts)/1000,cn_tz()).strftime('%Y-%m-%d %H:%M')
    except (TypeError,ValueError,OSError,OverflowError):
        return ''


class CninfoSource(AnnouncementSource):
    name='cninfo'

    def __init__(self,market=None):
        super().__init__(market)
        cfg=get_fetch_config().get('cninfo',{})
        self.columns=list(cfg.get('columns') or ['sse','szse'])
        if any(x not in ('sse','szse','bse') for x in self.columns) or len(set(self.columns))!=len(self.columns):
            raise ValueError('cninfo.columns 含无效或重复交易所')
        self.page_workers=max(1,min(4,int(cfg.get('workers',4))))
        self.max_pages=max(1,int(cfg.get('max_pages_per_day',1000)))

    def _fetch_page(self,column,se_date,page):
        r=self.market.http.post_form_json(CNINFO_URL,{'pageNum':page,'pageSize':PAGE_SIZE,
            'column':column,'tabName':'fulltext','plate':'','stock':'','searchkey':'','secid':'',
            'category':'','trade':'','seDate':se_date,'sortName':'','sortType':'','isHLtitle':'false'},
            {'Referer':'https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search'})
        if not isinstance(r,dict) or 'totalAnnouncement' not in r or int(r['totalAnnouncement'])<0:
            raise SourceError('巨潮公告接口返回无效响应')
        if r.get('announcements') is None and int(r['totalAnnouncement'])==0:
            r['announcements']=[]
        if not isinstance(r.get('announcements'),list):
            raise SourceError('巨潮公告列表无效')
        return r

    def _fetch_range(self,column,se_date):
        first=self._fetch_page(column,se_date,1)
        total=int(first['totalAnnouncement'])
        pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
        records={}
        errors=[]
        def put(response):
            if int(response['totalAnnouncement'])!=total:
                errors.append('翻页期间总数变化')
            for item in response['announcements']:
                key=(str(item.get('announcementId') or ''),str(item.get('secCode') or ''))
                if not key[0]:
                    key=(hashlib.sha256(str(sorted(item.items())).encode()).hexdigest(),key[1])
                records[key]=item
        put(first)
        def work(page):
            try:
                return self._fetch_page(column,se_date,page),''
            except Exception as exc:
                return None,f'{column} 第{page}页失败: {exc}'
        with ThreadPoolExecutor(max_workers=self.page_workers) as ex:
            for response,error in ex.map(work,range(2,min(pages,self.max_pages)+1)):
                if error: errors.append(error)
                else: put(response)
        if pages>self.max_pages or len(records)!=total:
            errors.append(f'{column} 唯一公告 {len(records)}/{total}，可能截断')
        return list(records.values()),total,errors

    def fetch_days(self,start,end):
        for day in date_range(start,end):
            result=DayResult(day,self.name,expected=0)
            errors=[]
            seen=set()
            for column in self.columns:
                try:
                    records,total,column_errors=self._fetch_range(column,f'{day}~{day}')
                    result.fetched+=len(records)
                    result.expected+=total
                    errors.extend(column_errors)
                    for raw in records:
                        code=str(raw.get('secCode') or '').strip()
                        if not valid_code(code):
                            continue
                        title=html.unescape(re.sub(r'<[^>]*>','',str(raw.get('shortTitle') or raw.get('announcementTitle') or ''))).strip()
                        dt=fmt_time(raw.get('announcementTime'))[:10]
                        if not title or dt!=day:
                            errors.append(f'{column} 公告标题为空或日期越界')
                            continue
                        aid=str(raw.get('announcementId') or hashlib.sha256(f'{code}|{title}|{dt}'.encode()).hexdigest())
                        aid=f'cninfo:{aid}#{code}'
                        if aid in seen: continue
                        seen.add(aid)
                        adjunct=str(raw.get('adjunctUrl') or '')
                        url='https://static.cninfo.com.cn/'+adjunct.lstrip('/') if adjunct else ''
                        if urlsplit(adjunct).scheme:
                            url=adjunct if urlsplit(adjunct).scheme=='https' and urlsplit(adjunct).hostname=='static.cninfo.com.cn' else ''
                        result.announcements.append(RawAnnouncement(aid,code,str(raw.get('secName') or ''),title,dt,board_of(code),url=url))
                except Exception as exc:
                    errors.append(f'{column} 失败: {exc}')
                    result.expected=None
                    break
            result.complete=not errors
            result.error='; '.join(dict.fromkeys(errors))[:1000]
            self.prefetch_market_caps(a.code for a in result.announcements)
            for ann in result.announcements:
                ann.market_value=self.market_cap(ann.code)
            yield result

    def probe_day_counts(self,days):
        out={}
        for day in days:
            try:
                out[day]=sum(int(self._fetch_page(c,f'{day}~{day}',1)['totalAnnouncement']) for c in self.columns)
            except Exception:
                pass
        return out
