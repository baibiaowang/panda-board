import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app import board, config, db, pipeline, site, store, window
from app.cli import main as cli_main
from app.locking import writer_lock
from app.maintenance import snapshot
from app.sources.codes import valid_code
from app.sources.mock import MockSource
from app.sources.types import DayResult, RawAnnouncement, RawKline
from app.taxonomy import Taxonomy
from app.timeutil import today_cn


def ann(aid='a', day='2026-09-10', code='600000', title='关于重大资产重组的进展公告'):
    return RawAnnouncement(aid, code, '测试股份', title, day, '主板', 100)


def bar(day, close=10, code='600000', source='eastmoney', adjustment='qfq'):
    return RawKline(code, day, close, close+1, close-1, close, 100, None, source, adjustment)


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'BOARD_DATA_DIR': str(self.base/'data'),
            'BOARD_SITE_DIR': str(self.base/'site'), 'BOARD_FETCHER': 'mock', 'BOARD_REQUIRE_VOLUME': '0'})
        self.env.start()
        db.close()
        db.init_db()
        self.engine = Taxonomy()

    def tearDown(self):
        db.close(); self.env.stop(); self.tmp.cleanup()


class StorageTests(DatabaseCase):
    def test_partial_refetch_preserves_previous_records(self):
        store.save_day(DayResult('2026-09-10', 'mock', [ann('a'), ann('b')], 2, 2, True), self.engine)
        store.save_day(DayResult('2026-09-10', 'mock', [ann('a')], 1, 2, False, 'page 2 failed'), self.engine)
        self.assertEqual(db.scalar('SELECT COUNT(*) FROM announcements'), 2)
        self.assertEqual(db.scalar('SELECT complete FROM fetch_days_v2'), 0)

    def test_failed_day_is_atomic(self):
        records = [ann('a'), ann('bad', code='6880001')]
        with self.assertRaises(ValueError):
            store.save_day(DayResult('2026-09-10', 'mock', records, 2, 2, True), self.engine)
        self.assertEqual(db.scalar('SELECT COUNT(*) FROM announcements'), 0)
        self.assertEqual(db.scalar('SELECT COUNT(*) FROM fetch_days_v2'), 0)

    def test_conflicting_complete_counts_rejected(self):
        with self.assertRaises(ValueError):
            store.save_day(DayResult('2026-09-10', 'mock', [], 0, 99, True), self.engine)

    def test_ann_upsert_and_idempotence(self):
        result = DayResult('2026-09-10', 'mock', [ann()], 1, 1, True)
        self.assertEqual(store.save_day(result, self.engine)['new'], 1)
        self.assertEqual(store.save_day(result, self.engine)['changed'], 0)
        result.announcements[0].title = '关于重大资产重组的更正公告'
        changed = store.save_day(result, self.engine)
        self.assertEqual((changed['new'], changed['changed']), (0, 1))

    def test_zero_market_cap_cannot_erase_known_value(self):
        store.save_stocks([('600000','测试','主板',123,'2026-09-10')])
        store.save_stocks([('600000','','主板',0,'2026-09-11')])
        self.assertEqual(db.scalar('SELECT market_value FROM stocks'), 123)
        self.assertEqual(db.scalar('SELECT name FROM stocks'), '测试')

    def test_noise_is_retained_in_database(self):
        a = ann(title='关于修订公司章程的公告')
        store.save_day(DayResult(a.date, 'mock', [a], 1, 1, True), self.engine)
        self.assertEqual(db.scalar('SELECT COUNT(*) FROM announcements'), 1)
        self.assertEqual(db.scalar('SELECT is_noise FROM announcements'), 1)

    def test_risk_announcement_is_not_noise(self):
        self.assertFalse(self.engine.is_noise('关于审计报告导致股票可能被终止上市的风险提示公告'))

    def test_corrected_kline_is_updated(self):
        def row(close): return ('600000','2026-09-10',close,close+1,close-1,close,10,None)
        store.save_klines([row(10)])
        store.save_klines([row(22)])
        self.assertEqual(db.scalar('SELECT close FROM klines'), 22)

    def test_snapshot_rebases_history_and_hides_old_provider_tail(self):
        store.save_kline_snapshot('600000', [bar('2026-09-09'),bar('2026-09-10')], '2026-09-10')
        store.save_kline_snapshot('600000', [bar('2026-09-10',20,source='tencent')], '2026-09-10')
        self.assertEqual(db.scalar('SELECT COUNT(*) FROM klines'), 2)
        self.assertEqual(db.scalar('SELECT COUNT(*) FROM current_klines'), 1)
        self.assertEqual(db.scalar('SELECT source FROM current_klines'), 'tencent')
        self.assertEqual(db.scalar('SELECT close FROM current_klines'), 20)

    def test_snapshot_rejects_mixed_adjustments(self):
        with self.assertRaises(ValueError):
            store.save_kline_snapshot('600000', [bar('2026-09-09'),bar('2026-09-10', adjustment='none')], '2026-09-10')

    def test_snapshot_rejects_nan_and_preserves_previous(self):
        store.save_kline_snapshot('600000', [bar('2026-09-10')], '2026-09-10')
        with self.assertRaises(ValueError):
            store.save_kline_snapshot('600000', [bar('2026-09-10',float('nan'))], '2026-09-10')
        self.assertEqual(db.scalar('SELECT close FROM current_klines'), 10)

    def test_same_snapshot_is_idempotent(self):
        rows = [bar('2026-09-10')]
        self.assertEqual(store.save_kline_snapshot('600000',rows,'2026-09-10'), 1)
        self.assertEqual(store.save_kline_snapshot('600000',rows,'2026-09-10'), 0)

    def test_db_connection_tracks_changed_data_path(self):
        store.meta_set('test','old')
        with patch.dict(os.environ, {'BOARD_DATA_DIR':str(self.base/'other')}):
            db.init_db()
            self.assertIsNone(store.meta_get('test'))
        self.assertEqual(store.meta_get('test'), 'old')

    def test_nested_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with db.transaction():
                store.meta_set('x','x')
                raise RuntimeError('fail')
        self.assertIsNone(store.meta_get('x'))

    def test_wal_backup_includes_uncheckpointed_data(self):
        c = db.conn()
        c.execute('PRAGMA wal_autocheckpoint=0')
        store.meta_set('wal-test','present')
        dest = snapshot(self.base/'data/board.db',self.base/'backup.db')
        # ★ sqlite3 的 with 只提交事务，不关闭连接。不显式 close，
        #   Windows 上句柄一直被占着，tearDown 清临时目录会 PermissionError。
        copy = sqlite3.connect(dest)
        try:
            self.assertEqual(copy.execute("SELECT value FROM board_meta WHERE key='wal-test'").fetchone()[0], 'present')
        finally:
            copy.close()

    def test_legacy_migration_takes_backup_and_keeps_data(self):
        db.close()
        with patch.dict(os.environ, {'BOARD_DATA_DIR':str(self.base/'legacy')}):
            path = self.base/'legacy/board.db';path.parent.mkdir()
            c = sqlite3.connect(path)
            try:
                for sql in db.SCHEMA: c.execute(sql)
                c.execute("INSERT INTO stocks(code,name) VALUES('600000','旧数据')")
                # ★ 原来靠 `with` 的 __exit__ 隐式提交。改成显式 close 后必须自己 commit，
                #   否则事务回滚、数据根本没落盘（with 会提交，close 不会）。
                c.commit()
            finally:
                c.close()
            db.init_db()
            self.assertEqual(db.scalar('SELECT name FROM stocks'), '旧数据')
            self.assertEqual(db.scalar('SELECT version FROM schema_version'), 2)
            self.assertEqual(len(list(path.parent.glob('backups/pre-v2-*.db'))), 1)
            db.init_db()
            self.assertEqual(len(list(path.parent.glob('backups/pre-v2-*.db'))), 1)

class WindowBoardTests(DatabaseCase):
    def test_one_day_is_one_day(self):
        self.assertEqual(window.resolve_incremental('2026-09-10',1)[:2], ('2026-09-10','2026-09-10'))
        self.assertEqual(window.date_range('2026-09-10','2026-09-10'), ['2026-09-10'])

    def test_backlog_does_not_starve_current_days(self):
        days = window.plan_dates('2026-09-10',2,'mock',90,12)
        self.assertEqual(len(days), 12)
        self.assertIn('2026-09-10',days)
        self.assertIn('2026-09-09',days)
        self.assertEqual(days[:2],['2026-09-09','2026-09-10'])

    def test_confirmed_zero_is_not_missing_even_on_weekend(self):
        end = today_cn()
        start = end-timedelta(days=6)
        for d in window.date_range(start.isoformat(),end.isoformat()):
            store.save_day(DayResult(d,'mock',[],0,0,True),self.engine)
        gaps = board.detect_gaps(7)
        self.assertEqual(gaps['coverage'],100)
        self.assertEqual(len(gaps['confirmed_empty_days']),7)

    def test_unknown_and_failed_are_distinct(self):
        today = today_cn().isoformat()
        store.save_day(DayResult(today,'mock',error='timeout'),self.engine)
        result = board.detect_gaps(2)
        self.assertEqual(result['incomplete_days'],[today])
        self.assertEqual(len(result['missing_days']),1)

    def test_event_return_uses_passed_window_anchor(self):
        rows = [bar('2026-09-07',5),bar('2026-09-08',10),bar('2026-09-09',20),bar('2026-09-10',30)]
        store.save_kline_snapshot('600000',rows,'2026-09-10')
        stats = board._chg_stats(['600000'],{'600000':'2026-09-09'})
        self.assertEqual(stats['600000']['chg_ann'],200)
        self.assertEqual(stats['600000']['chg'],50)
        self.assertIsNone(stats['600000']['chg5'])

    def test_event_return_without_previous_close_is_unknown(self):
        store.save_kline_snapshot('600000',[bar('2026-09-10')],'2026-09-10')
        self.assertIsNone(board._chg_stats(['600000'],{'600000':'2026-09-09'})['600000']['chg_ann'])

    def test_invalid_codes_and_mock_stability(self):
        for c in ['6880001','600000x','abc','900001','60000']:
            self.assertFalse(valid_code(c),c)
        src=MockSource(pool_size=10,ann_per_day=10)
        self.assertTrue(all(valid_code(x['code']) for x in src._pool))
        left={r.date:r.close for r in src.klines('600000','','2026-09-10')}
        right={r.date:r.close for r in src.klines('600000','','2026-09-11')}
        self.assertTrue(all(left[d]==right[d] for d in left.keys() & right.keys()))

    def test_full_pipeline_repeat_and_static_artifact(self):
        end=today_cn().isoformat();start=(today_cn()-timedelta(days=2)).isoformat()
        first=pipeline.run_pipeline('full',start,end,source=MockSource(pool_size=12,ann_per_day=15))
        self.assertTrue(first['ok'],first)
        self.assertGreater(first['new'],0)
        second=pipeline.run_pipeline('full',start,end,source=MockSource(pool_size=12,ann_per_day=15))
        self.assertTrue(second['ok'],second)
        self.assertEqual(second['new'],0)
        result=site.build(str(self.base/'site'))
        self.assertTrue(result['ok'],result)
        from app.validator import verify_artifact
        self.assertTrue(verify_artifact(self.base/'site')['ok'])
        html=(self.base/'site/index.html').read_text()
        self.assertRegex(html,r'dashboard\.[0-9a-f]{16}\.js')
        self.assertIn('"files"',(self.base/'site/data_kline_manifest.js').read_text())

    def test_build_failure_preserves_previous_artifact(self):
        site.build(str(self.base/'site'))
        before=(self.base/'site/artifact.json').read_bytes()
        with patch('app.site.build_kline_shards',side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError): site.build(str(self.base/'site'))
        self.assertEqual(before,(self.base/'site/artifact.json').read_bytes())

    def test_build_rejects_arbitrary_existing_directory(self):
        target=self.base/'notes';target.mkdir();(target/'important.txt').write_text('keep')
        with self.assertRaises(ValueError): site.build(str(target))
        self.assertEqual((target/'important.txt').read_text(),'keep')
        site.build(str(self.base/'site'))
        note=self.base/'site/my-notes.txt';note.write_text('keep too')
        with self.assertRaises(ValueError): site.build(str(self.base/'site'))
        self.assertEqual(note.read_text(),'keep too')

    def test_pipeline_partial_is_not_success(self):
        class Broken(MockSource):
            def fetch_days(self,start,end):
                yield DayResult(start,'mock',error='page failed')
        d=today_cn().isoformat()
        result=pipeline.run_pipeline('full',d,d,source=Broken(pool_size=2),with_klines=False)
        self.assertFalse(result['ok'])
        self.assertTrue(result['publishable'])
        self.assertEqual(db.scalar('SELECT status FROM runs'),'partial')
        retry_dates=['2026-09-09','2026-09-10','2026-08-01']
        with patch('app.pipeline.run_pipeline',side_effect=[{'ok':False,'dates':retry_dates},{'ok':True}]) as run:
            pipeline.run_with_retry(max_attempts=2,delay_seconds=0)
        self.assertEqual(run.call_args_list[1].kwargs['planned_dates'],retry_dates)

    def test_config_errors_fail_closed(self):
        with self.assertRaises(ValueError): config.validate_config({})
        cfg=config.rules_config();cfg['classify']['multi_label']=True
        with self.assertRaises(ValueError): config.validate_config(cfg)


if __name__=='__main__': unittest.main()
