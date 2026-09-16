"""命令行入口：采集、校验、建站、完整更新（cycle）、诊断。

★ 本项目只有一条发布路径：`cycle`。它 clone 数据仓库 → 导入临时 SQLite →
  采集 → 校验 → 建站 → 导出 → 提交。任何一步失败就不提交，
  GitHub Pages 上永远保留上一份可用版本。

  不存在"推到某个独立分支"的第二条路，也没有常驻 worker 需要管理。
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from . import board,cycle,db,paths,pipeline,site,store
from .config import rules_config
from .locking import writer_lock,BusyError
from .timeutil import now_cn,tz_report


def log(message):
    print(f'[{now_cn().isoformat(timespec="seconds")}] {message}',flush=True)


def emit(result):
    print(json.dumps(result,ensure_ascii=False,allow_nan=False,default=str),flush=True)


def cmd_check(args):
    rules_config()
    db.init_db()
    integrity=db.scalar('PRAGMA quick_check')
    free=shutil.disk_usage(paths.data_dir()).free
    return {'ok':integrity=='ok','data_dir':paths.data_dir(),
        'db_stats':db.table_stats(),'integrity':integrity,'free_mb':free//1048576,'timezone':tz_report()}


def cmd_gaps(args):
    db.init_db()
    result=board.detect_gaps(args.days,args.thin_below)
    return {**result,'ok':result['verdict']=='ok'}


def cmd_run(args):
    result=pipeline.run_update(mode=args.mode,start=args.start,end=args.end,lookback_days=args.lookback_days,
        max_attempts=args.max_attempts,retry_delay=args.retry_delay)
    log(f"采集 {result.get('status')}：公告新增 {result.get('new',0)}，"
        f"K线变更 {result.get('klines',0)}，市值 {result.get('market_caps',0)}")
    return result


def cmd_build(args):
    return site.build(args.site or paths.site_dir(),args.days)


def cmd_validate(args):
    from .validator import check_data
    result=check_data(args.days)
    for item in result.get('checks',[]):
        log(('  OK   ' if item['ok'] else ' FAIL  ')+item['name']
            +('  '+item['detail'] if item['detail'] else ''))
    return result


def cmd_cycle(args):
    return cycle.run(args.data_repo,mode=args.mode,lookback_days=args.lookback_days,
        max_attempts=args.max_attempts,retry_delay=args.retry_delay,
        repo=args.repo,token=args.token,branch=args.branch,days=args.days,push=not args.no_push)


def cmd_reclassify(args):
    from .taxonomy import Taxonomy
    engine=Taxonomy()
    db.init_db()
    with db.transaction() as c:
        rows=c.execute('SELECT id,title,summary FROM announcements').fetchall()
        updates=[]
        for row in rows:
            summary=row['summary'] or ''
            updates.append((engine.classify(row['title'],summary),int(engine.is_noise(row['title'])),
                ','.join(engine.extract_numbers(row['title']+' '+summary)),row['id']))
        c.executemany('UPDATE announcements SET category=?,is_noise=?,key_numbers=? WHERE id=?',updates)
        count=len(updates)
    return {'ok':True,'reclassified':count,
            'note':'重分类不会恢复已过滤删除的公告；需要重新抓取历史区间'}


def main(argv=None):
    ap=argparse.ArgumentParser(description='A股公告看板 · GitHub + PandaStack')
    sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('check').set_defaults(fn=cmd_check)
    p=sub.add_parser('gaps');p.add_argument('--days',type=int,default=90)
    p.add_argument('--thin-below',type=int,default=5);p.set_defaults(fn=cmd_gaps)
    p=sub.add_parser('validate');p.add_argument('--days',type=int,default=90);p.set_defaults(fn=cmd_validate)
    for cmd,fn in [('run',cmd_run),('build',cmd_build),('cycle',cmd_cycle)]:
        p=sub.add_parser(cmd);p.set_defaults(fn=fn)
        if cmd in ('run','cycle'):
            p.add_argument('--mode',choices=['incremental','full'],default='incremental')
            p.add_argument('--start');p.add_argument('--end');p.add_argument('--lookback-days',type=int)
            p.add_argument('--max-attempts',type=int,default=2);p.add_argument('--retry-delay',type=int,default=10)
        if cmd=='build':
            p.add_argument('--days',type=int,default=90);p.add_argument('--site')
        if cmd=='cycle':
            p.add_argument('--data-repo',required=True,help='数据仓库的本地目录')
            p.add_argument('--repo');p.add_argument('--token');p.add_argument('--branch',default='main')
            p.add_argument('--days',type=int,default=90)
            p.add_argument('--no-push',action='store_true',help='只构建不提交，用于本地验证')
    sub.add_parser('reclassify').set_defaults(fn=cmd_reclassify)
    args=ap.parse_args(argv)
    try:
        with writer_lock():
            try: result=args.fn(args)
            finally: db.close()
        emit(result)
        return 0 if result.get('ok') else 1
    except Exception as exc:
        emit({'ok':False,'error':f'{type(exc).__name__}: {exc}','conflict':isinstance(exc,BusyError)})
        return 1
    finally:
        db.close()


if __name__=='__main__':
    sys.exit(main())
