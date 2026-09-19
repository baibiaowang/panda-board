import json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
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

class CommitPushTest(unittest.TestCase):
    """commit_push 的回归。

    ★ 2026-09-19 事故：`_git()` 给所有命令统一 timeout=300，而
      `git commit` 在 5000+ 文件的仓库上会触发 auto-gc（detach 但继承
      管道 → subprocess 读不到 EOF），被 SIGTERM 杀掉后 `commit_push()`
      中止、**push 一行都没跑** —— 线上表现是“提交成功但没推上去”。
      这两条用例分别锁住“真的推上去了”和“自动维护已关闭 + 超时按操作分档”。
    """

    def setUp(self):
        self.td=tempfile.TemporaryDirectory()
        self.remote=Path(self.td.name)/'remote.git'
        self.work=Path(self.td.name)/'work'
        subprocess.run(['git','init','--bare',str(self.remote)],check=True,capture_output=True)
        subprocess.run(['git','init',str(self.work)],check=True,capture_output=True)
        # ★ 必须给工作仓配好 origin：commit_push 里是 `git push origin ...`，
        #   没有远端就会报 "Please make sure you have the correct access rights
        #   and the repository exists."（v1 漏了这一步，测试自己挂的）
        subprocess.run(['git','-C',str(self.work),'remote','add','origin',str(self.remote)],
                       check=True,capture_output=True)
        # 关掉签名，免测试结果被本机全局 git 配置左右
        subprocess.run(['git','-C',str(self.work),'config','commit.gpgsign','false'],
                       check=True,capture_output=True)

    def tearDown(self):
        self.td.cleanup()

    def test_commit_push_reaches_remote(self):
        (self.work/'a.txt').write_text('hello',encoding='utf-8')
        out=github_store.commit_push(str(self.work),'owner/repo','','chore: test')
        self.assertTrue(out['ok'],out)
        self.assertTrue(out['changed'])
        head=subprocess.run(['git','-C',str(self.remote),'rev-parse','refs/heads/main'],
            check=True,capture_output=True,text=True).stdout.strip()
        self.assertEqual(head,out['commit'])
        # 没有新变化时再跑一次：changed=False，且不能抛异常
        again=github_store.commit_push(str(self.work),'owner/repo','','chore: test')
        self.assertEqual(again['changed'],False)

    def test_git_disables_auto_maintenance_and_splits_timeouts(self):
        seen={}
        def fake_run(cmd,**kwargs):
            seen['cmd']=list(cmd); seen['timeout']=kwargs.get('timeout')
            return subprocess.CompletedProcess(cmd,0,'','')
        with patch('app.github_store.subprocess.run',side_effect=fake_run):
            github_store._git(self.work,'status')
            default=seen['timeout']
            github_store._git(self.work,'commit','-m','x',timeout=github_store.GIT_TIMEOUT_TREE)
            tree=seen['timeout']
        self.assertIn('gc.auto=0',seen['cmd'])
        self.assertIn('maintenance.auto=false',seen['cmd'])
        self.assertGreater(tree,default)
        self.assertGreater(github_store.GIT_TIMEOUT_TREE,github_store.GIT_TIMEOUT_DEFAULT)
        self.assertGreaterEqual(github_store.GIT_TIMEOUT_NET,github_store.GIT_TIMEOUT_TREE)


if __name__=='__main__': unittest.main()
