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


class DataLayerV2Tests(DatabaseCase):
    """数据层 v2（2026-09-19）：build_data() + verify_data_dir() 的契约。

    ★ 改造前这条路径零覆盖 —— 只有已废弃的 build()/verify_artifact() 被测过，
      所以数据层换了格式也没人拦得住。这组用例就是补这个缺口。
    """

    def _seed(self, days=3, pool_size=12, ann_per_day=15):
        end = today_cn().isoformat()
        start = (today_cn() - timedelta(days=days-1)).isoformat()
        result = pipeline.run_pipeline('full', start, end,
                                       source=MockSource(pool_size=pool_size, ann_per_day=ann_per_day))
        self.assertTrue(result['ok'], result)

    def _build(self):
        return site.build_data(str(self.base/'site'), 90, repo_root=str(self.base))

    def _read(self, *parts):
        return json.loads((self.base/'site'/'data').joinpath(*parts).read_text(encoding='utf-8'))

    def test_build_data_installs_and_verifies(self):
        self._seed()
        out = self._build()
        self.assertTrue(out.get('ok'), out)
        for name in ('meta.json','home.json','artifact.json',
                     'list-main.json','list-gem.json','list-star.json',
                     'list-bse.json','list-st.json','list-all.json'):
            self.assertTrue((self.base/'site'/'data'/name).is_file(), name)
        meta = self._read('meta.json')
        self.assertEqual(meta['v'], 2)
        self.assertEqual(meta['home_range_days'], 3)
        self.assertTrue(meta['taxonomy'])
        self.assertEqual(meta['counts']['stocks'], out['stocks'])
        self.assertEqual(self._read('artifact.json')['schema'], 2)
        for key in ('main','gem','star','bse','st','all'):
            obj = self._read(f'list-{key}.json')
            self.assertEqual(obj['count'], len(obj['items']))
            self.assertEqual(obj['count'], meta['counts'][key])
            for entry in obj['items'][:50]:
                self.assertEqual(len(entry), 13)
                self.assertIsInstance(entry[12], list)
                for a in entry[12]:
                    self.assertEqual(len(a), 3)   # 档位里的公告不带 URL
        home = self._read('home.json')
        self.assertEqual(home['count'], len(home['items']))
        self.assertEqual(home['count'], meta['counts']['home'])

    def test_stock_detail_carries_kline_and_urls(self):
        self._seed()
        out = self._build()
        self.assertTrue(out.get('ok'), out)
        stocks = sorted((self.base/'site'/'data'/'stock').rglob('*.json'))
        self.assertEqual(len(stocks), out['stocks'])
        for p in stocks[:5]:
            obj = json.loads(p.read_text(encoding='utf-8'))
            self.assertEqual(obj['v'], 2)
            self.assertIsInstance(obj['k'], list)
            self.assertIsInstance(obj['a'], list)
            for a in obj['a']:
                self.assertEqual(len(a), 4)   # [date,title,category_id,url]

    def test_verify_data_dir_rejects_tampered_file(self):
        from app.validator import verify_data_dir
        self._seed()
        self.assertTrue(self._build().get('ok'))
        self.assertTrue(verify_data_dir(self.base/'site'/'data')['ok'])
        target = self.base/'site'/'data'/'list-main.json'
        target.write_text(target.read_text(encoding='utf-8')+' ', encoding='utf-8')
        self.assertFalse(verify_data_dir(self.base/'site'/'data')['ok'])

    def test_empty_home_is_allowed(self):
        """长假里「近 3 天没有主板非 ST 公告」是合法状态，不能因此让整站停更。"""
        from app.validator import verify_data_dir
        old = (today_cn() - timedelta(days=40)).isoformat()
        # 只在 40 天前那天有公告：展示窗口内仍有数据，但近 3 天一条都没有。
        store.save_day(DayResult(old, 'mock', [ann(day=old)], 1, 1, True), self.engine)
        store.save_kline_snapshot('600000', [bar(old)], old)
        out = self._build()
        self.assertTrue(out.get('ok'), out)
        home = self._read('home.json')
        self.assertEqual(home['count'], 0)
        self.assertTrue(self._read('list-main.json')['count'] > 0)
        self.assertTrue(verify_data_dir(self.base/'site'/'data')['ok'])

    def test_ai_csv_lands_in_data_repo_not_code_repo(self):
        from app.site import _db_b_root
        self._seed()
        out = self._build()
        self.assertTrue(out.get('ok'), out)
        csv = self.base/'ai'/'stocks.csv'
        self.assertTrue(csv.is_file())
        lines = csv.read_text(encoding='utf-8').strip().split('\n')
        self.assertEqual(lines[0], 'code,name,category')
        self.assertEqual(out['ai']['rows'], len(lines)-1)
        # 不给 repo_root 时绝不能默认落到源码仓（否则 CSV 写进源码树、永远推不出去）
        with self.assertRaises(ValueError):
            _db_b_root(Path(site.BASE_DIR)/'dist', None)

    def test_db_b_csv_uses_chinese_labels(self):
        from app.site import _write_ai_csv
        taxonomy = [{'id':'merger','label':'并购重组'},{'id':'personnel','label':'人事变动'}]
        items = [{'code':'600000','name':'测试股份',
                  'announcements':[{'category_id':'merger'},{'category_id':'personnel'},
                                   {'category_id':'merger'}]}]
        info = _write_ai_csv(self.base, items, taxonomy)
        lines = (self.base/'ai'/'stocks.csv').read_text(encoding='utf-8').strip().split('\n')
        self.assertEqual(lines, ['code,name,category','600000,测试股份,并购重组','600000,测试股份,人事变动'])
        self.assertEqual(info['rows'], 2)   # 同类只出一行


class ShellLayerTests(DatabaseCase):
    """固定层（build_shell + verify_shell）的回归。

    ★ 这条路径此前零覆盖 —— 只有已废弃的 build()/verify_artifact() 被测过，于是
      build_shell 把 artifact.json 自己的**过期** sha256 写进清单这件事一直没人
      拦得住（改完前端跑标准 `cli build-shell` 会报「产物损坏: artifact.json」）。
      第二次调用是关键：只有此时磁盘上已有上一版 artifact.json，缺陷才会现形。
    """

    def test_build_shell_is_repeatable_and_self_consistent(self):
        from app.validator import verify_shell
        target = self.base/'site'
        site.build_shell(str(target))
        first = verify_shell(target)
        self.assertTrue(first['ok'], first)
        site.build_shell(str(target))
        second = verify_shell(target)
        self.assertTrue(second['ok'], second)

        manifest = json.loads((target/'artifact.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['kind'], 'shell')
        # 清单里绝不能有 artifact.json 自己（自引用 → 覆盖后 sha 必然对不上）
        self.assertNotIn('artifact.json', manifest['files'])
        for name in ('index.html', 'dashboard.html', 'dashboard.js',
                     'lib/echarts.min.js', 'robots.txt', '.nojekyll'):
            self.assertIn(name, manifest['files'])
        # 产物全集 = 清单里的文件 + artifact.json 自己
        actual = {p.relative_to(target).as_posix() for p in target.rglob('*')
                  if p.is_file() and 'data' not in p.relative_to(target).parts}
        self.assertEqual(actual, set(manifest['files']) | {'artifact.json'})
        self.assertEqual(first['bytes'], second['bytes'])


if __name__=='__main__': unittest.main()
