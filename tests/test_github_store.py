import json, os, tempfile, unittest
from pathlib import Path
from app import db, github_store

class GitHubStoreTest(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory()
        self.data=Path(self.td.name)/'work'
        self.repo=Path(self.td.name)/'repo'
        os.environ['BOARD_DATA_DIR']=str(self.data)
        db.close(); db.init_db()
    def tearDown(self):
        db.close(); os.environ.pop('BOARD_DATA_DIR',None); self.td.cleanup()
    def test_export_import(self):
        with db.transaction() as c:
            c.execute("INSERT INTO stocks(code,name,board,market_value,updated_at) VALUES('600000','浦发银行','主板',100.0,'2026-09-16')")
            c.execute("INSERT INTO announcements(id,ann_id,code,name,title,date,category,board,key_numbers,url,summary,created_at,source,is_noise) VALUES(1,'a1','600000','浦发银行','测试公告','2026-09-16','x','主板','','https://example.invalid','', '2026-09-16','mock',0)")
            c.execute("INSERT INTO klines(id,code,date,open,high,low,close,volume,change_pct,source,adjustment,snapshot_key) VALUES(1,'600000','2026-09-16',1,2,0.9,1.5,10,1,'mock','qfq','s1')")
        out=github_store.export_data(str(self.repo))
        self.assertEqual(out['stocks'],1)
        db.close(); os.remove(self.data/'board.db')
        db.init_db(); imported=github_store.import_data(str(self.repo))
        self.assertEqual(imported['announcements'],1)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM klines"),1)
        self.assertEqual(db.scalar("SELECT name FROM stocks WHERE code='600000'"),'浦发银行')

    def test_function_bundle(self):
        from function import deploy
        import io, tarfile
        with tarfile.open(fileobj=io.BytesIO(deploy.build_bundle()), mode='r:gz') as t:
            self.assertEqual(set(t.getnames()), {'handler.py','panda_api.py'})

if __name__=='__main__': unittest.main()
