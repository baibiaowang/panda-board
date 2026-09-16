import json
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch
from test_core import DatabaseCase
from app.sources.eastmoney import EastmoneySource
from app.sources.cninfo import CninfoSource
from app.sources.quotes import MarketData
from app.sources.types import SourceError
from app.timeutil import cn_tz
from datetime import datetime


def em_doc(n, codes=('600000',), day='2026-09-10'):
    return {'art_code':f'AN{n}', 'title':'关于重大资产重组的公告', 'notice_date':day,
            'codes':[{'stock_code':code,'short_name':'测试'} for code in codes]}


def em_page(total, docs):
    return {'success':1,'data':{'total_hits':total,'list':docs}}


class AnnouncementTests(DatabaseCase):
    def make_em(self, responses):
        market=SimpleNamespace(http=Mock())
        source=EastmoneySource(market)
        source.page_size=1;source.page_sleep=0;source.max_pages=10
        source._query=Mock(side_effect=responses)
        return source

    def test_multistock_expansion_not_compared_to_document_total(self):
        source=self.make_em([em_page(1,[em_doc(1,('600000','000001'))])])
        result=source._fetch_day('2026-09-10')
        self.assertTrue(result.complete,result.error)
        self.assertEqual((result.fetched,result.expected,len(result.announcements)),(1,1,2))

    def test_duplicate_page_detected_by_unique_documents(self):
        source=self.make_em([em_page(2,[em_doc(1)]),em_page(2,[em_doc(1)])])
        result=source._fetch_day('2026-09-10')
        self.assertFalse(result.complete)
        self.assertEqual(result.fetched,1)

    def test_page_failure_keeps_partial_records_and_flags_day(self):
        source=self.make_em([em_page(2,[em_doc(1)]),TimeoutError('network')])
        result=source._fetch_day('2026-09-10')
        self.assertFalse(result.complete)
        self.assertEqual(len(result.announcements),1)
        self.assertEqual(result.expected,2)

    def test_page_cap_is_never_complete(self):
        source=self.make_em([em_page(4,[em_doc(1)])]);source.max_pages=1
        self.assertFalse(source._fetch_day('2026-09-10').complete)

    def test_total_changes_during_pagination(self):
        source=self.make_em([em_page(2,[em_doc(1)]),em_page(3,[em_doc(2)])])
        self.assertFalse(source._fetch_day('2026-09-10').complete)

    def test_zero_source_day_is_complete(self):
        result=self.make_em([em_page(0,[])])._fetch_day('2026-09-10')
        self.assertTrue(result.complete)
        self.assertEqual(result.expected,0)

    def test_parse_failure_cannot_be_complete(self):
        source=self.make_em([em_page(1,[em_doc(1,day='2026-09-09')])])
        self.assertFalse(source._fetch_day('2026-09-10').complete)

    def test_probe_failure_is_unknown_not_zero(self):
        source=self.make_em([TimeoutError('fail')])
        self.assertEqual(source.probe_day_counts(['2026-09-10']),{})

    def test_cninfo_exchange_failure_not_successful_empty(self):
        market=SimpleNamespace(http=Mock(),prefetch_market_caps=lambda codes:None,market_cap=lambda code:0)
        source=CninfoSource(market)
        source._fetch_range=Mock(side_effect=[([],0,[]),TimeoutError('szse timeout')])
        result=list(source.fetch_days('2026-09-10','2026-09-10'))[0]
        self.assertFalse(result.complete)
        self.assertIsNone(result.expected)

    def test_cninfo_announcement_id_includes_stock(self):
        market=SimpleNamespace(http=Mock(),prefetch_market_caps=lambda codes:None,market_cap=lambda code:0)
        source=CninfoSource(market);source.columns=['sse']
        ms=int(datetime(2026,9,10,18,tzinfo=cn_tz()).timestamp()*1000)
        docs=[{'announcementId':'same','secCode':code,'announcementTitle':'重大资产重组',
            'announcementTime':ms,'secName':'测试'} for code in ['600000','600001']]
        source._fetch_range=Mock(return_value=(docs,2,[]))
        result=list(source.fetch_days('2026-09-10','2026-09-10'))[0]
        self.assertTrue(result.complete)
        self.assertEqual(len({x.ann_id for x in result.announcements}),2)


class QuoteTests(DatabaseCase):
    def market(self, replies, cfg=None):
        http=Mock();http.get_json.side_effect=replies
        return MarketData(cfg=cfg or {'market_cap':{'enabled':False}},http=http)

    def tx(self, rows, adjustment='qfqday'):
        return {'code':0,'data':{'sh600000':{adjustment:rows}}}

    def em(self, rows):
        return {'rc':0,'data':{'code':'600000','klines':[','.join(map(str,r)) for r in rows]}}

    def test_empty_plus_transient_failure_not_negative_cached(self):
        market=self.market([self.tx([]),TimeoutError('network')])
        with self.assertRaises(SourceError): market.klines('600000','','2026-09-10')
        self.assertEqual(market._nodata,{})

    def test_two_valid_empty_results_use_range_specific_cache(self):
        market=self.market([self.tx([]),self.em([])])
        self.assertEqual(market.klines('600000','','2026-09-10'),[])
        self.assertTrue(market._nodata_recent('600000','','2026-09-10'))
        self.assertFalse(market._nodata_recent('600000','','2026-09-11'))
        self.assertFalse(market._nodata_recent('600000','2025-01-01','2025-01-31'))

    def test_http_200_business_error_not_empty(self):
        market=self.market([{'code':1,'msg':'blocked'},{'rc':1,'data':None}])
        with self.assertRaises(SourceError): market.klines('600000','','2026-09-10')
        self.assertEqual(market._nodata,{})

    def test_first_visible_change_has_previous_close(self):
        rows=[['2026-09-09',10,10,11,9,100],['2026-09-10',11,11,12,10,120]]
        market=self.market([self.tx(rows)])
        result=market.klines('600000','2026-09-10','2026-09-10')
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].change_pct,10)
        url=market.http.get_json.call_args.args[0]
        self.assertIn('2026-09-10',parse_qs(urlsplit(url).query)['param'][0])

    def test_unadjusted_fallback_is_labelled(self):
        market=self.market([self.tx([['2026-09-10',10,10,11,9,100]],'day')])
        self.assertEqual(market.klines('600000','','2026-09-10')[0].adjustment,'none')

    def test_malformed_primary_uses_valid_fallback(self):
        market=self.market([self.tx([['2026-09-10',10,10,1,9,100]]),self.em([['2026-09-10',10,10,11,9,100]])])
        self.assertEqual(market.klines('600000','','2026-09-10')[0].source,'eastmoney')

    def test_primary_ignoring_historical_range_uses_fallback(self):
        market=self.market([self.tx([['2026-09-10',10,10,11,9,100]]),self.em([['2025-09-10',10,10,11,9,100]])])
        result=market.klines('600000','2025-09-01','2025-09-30')
        self.assertEqual(result[0].date,'2025-09-10')
        self.assertEqual(result[0].source,'eastmoney')

    def test_batch_errors_are_explicit(self):
        market=self.market([TimeoutError('x'),TimeoutError('y')])
        self.assertEqual(list(market.klines_batch(['600000'],'','2026-09-10')),[('600000',[])])
        self.assertIn('600000',market.kline_errors)
        self.assertEqual(market.kline_empty,set())

    def test_failed_market_cap_prefetch_only_attempted_once(self):
        market=self.market([],{'market_cap':{'enabled':True}})
        market.http.get.side_effect=TimeoutError('not available')
        market.prefetch_market_caps(['600000'])
        self.assertEqual(market.market_cap('600000'),0)
        self.assertEqual(market.market_cap('600000'),0)
        self.assertEqual(market.http.get.call_count,1)

    def test_reference_market_cap_cache_is_preserved(self):
        market=self.market([],{'market_cap':{'enabled':True,'cache_days':0}})
        market._mv['600000']={'v':123,'t':time.time()-365*86400}
        self.assertEqual(market.market_cap('600000'),123)
        market.http.get.assert_not_called()

    def test_close_always_closes_http_even_when_cache_save_fails(self):
        market=self.market([])
        with patch.object(market,'save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): market.close()
        market.http.close.assert_called_once()


if __name__=='__main__': unittest.main()
