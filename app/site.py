"""Build a complete version in staging; install only after integrity checks pass."""
from __future__ import annotations
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from . import board,db
from .config import get_build_config,site_title
from .locking import writer_lock
from .paths import site_dir,web_dir,data_dir,BASE_DIR
from .validator import verify_artifact,verify_data_dir

KLINE_SHARDS=16
KLINE_BARS=120


def shard_of(code,shards=KLINE_SHARDS):
    return int(code)%shards


def _write(path,content):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content,encoding='utf-8')
    return path.stat().st_size


def _hashed_copy(site,filename):
    p=Path(site)/filename
    digest=hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    target=p.with_name(p.stem+'.'+digest+p.suffix)
    shutil.copyfile(p,target)
    return target.relative_to(site).as_posix()


def build_kline_shards(site,bars,fingerprint=''):
    buckets={i:{} for i in range(KLINE_SHARDS)}
    rows=db.conn().execute('''SELECT code,date,open,close,high,low,volume FROM (
        SELECT *, ROW_NUMBER() OVER(PARTITION BY code ORDER BY date DESC) rn
        FROM current_klines) WHERE rn<=? ORDER BY code,date''',(bars,))
    for row in rows:
        buckets[shard_of(row['code'])].setdefault(row['code'],[]).append(
            [row['date'],row['open'],row['close'],row['high'],row['low'],row['volume']])
    files={}
    total=0
    for n,bucket in buckets.items():
        filename=f'data_kline_{n}.js'
        total+=_write(Path(site)/filename,f'window.ANNO_KLINE_SHARD_{n} = '+json.dumps(bucket,ensure_ascii=False,allow_nan=False,separators=(',',':'))+';')
        files[str(n)]=_hashed_copy(site,filename)
    manifest={'shards':KLINE_SHARDS,'codes':sum(len(b) for b in buckets.values()),'bars':bars,'files':files}
    _write(Path(site)/'data_kline_manifest.js','window.ANNO_KLINE_SHARDS = '+json.dumps(manifest,separators=(',',':'))+';')
    return {**manifest,'bytes':total,'reused':False}


def _safe_target(target):
    target=Path(target).absolute()
    if target.is_symlink(): raise ValueError('产物目录不能是符号链接')
    target=target.resolve()
    repo=Path(BASE_DIR).resolve()
    data=Path(data_dir()).resolve()
    if target==repo or target in repo.parents or target==data or target in data.parents or data in target.parents:
        raise ValueError('产物目录不能覆盖源码或持久数据')
    for protected in ('app','config','function','scripts','tools','web','tests'):
        p=repo/protected
        if target==p or p in target.parents: raise ValueError('产物目录不能位于源码子目录中')
    if target.exists() and (target/'artifact.json').is_file():
        try:
            previous=json.loads((target/'artifact.json').read_text(encoding='utf-8'))
            owned=set(previous['files'])|{'artifact.json'}
            actual={p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
            if actual-owned:
                raise ValueError('产物目录中有清单之外的文件，拒绝覆盖；请先移走用户文件')
        except (KeyError,TypeError,json.JSONDecodeError) as exc:
            raise ValueError('现有产物清单无效，拒绝覆盖目录') from exc
    if target.exists() and not (target/'artifact.json').is_file():
        known={'index.html','dashboard.html','data_list.js','data_kline_manifest.js','lib','robots.txt','.nojekyll'}|{f'data_kline_{i}.js' for i in range(KLINE_SHARDS)}
        if set(p.name for p in target.iterdir())-known:
            raise ValueError('现有目录包含非看板文件，拒绝替换')
    return target


def build(site=None,days=90,reuse_klines=True):
    target=_safe_target(site or site_dir())
    target.parent.mkdir(parents=True,exist_ok=True)
    with writer_lock():
        db.init_db()
        stage=Path(tempfile.mkdtemp(prefix='.board-build-',dir=target.parent))
        backup=None
        try:
            with db.transaction():
                payload=board.build_payload(days)
                bars=int(get_build_config().get('kline_bars',KLINE_BARS))
                if not 6<=bars<=640: raise ValueError('build.kline_bars 必须在6到640之间')
                kstats=build_kline_shards(stage,bars)
                shutil.copytree(Path(web_dir())/'lib',stage/'lib')
                payload['meta']['echarts_url']=_hashed_copy(stage,'lib/echarts.min.js')
                payload['meta']['kline_bars']=bars
                _write(stage/'data_list.js',board.render_js(payload))
            shutil.copyfile(Path(web_dir())/'dashboard.js',stage/'dashboard.js')
            script_url=_hashed_copy(stage,'dashboard.js')
            data_url=_hashed_copy(stage,'data_list.js')
            manifest_url=_hashed_copy(stage,'data_kline_manifest.js')
            html=(Path(web_dir())/'dashboard.html').read_text(encoding='utf-8')
            html=re.sub(r'<title>.*?</title>',lambda _: '<title>'+html_lib.escape(site_title())+'</title>',html,count=1)
            html=html.replace('<head>','<head>\n<meta name="robots" content="noindex,nofollow">',1)
            html=html.replace('src="dashboard.js"',f'src="{script_url}"')
            html=html.replace('src="data_list.js"',f'src="{data_url}"').replace('src="data_kline_manifest.js"',f'src="{manifest_url}"')
            _write(stage/'index.html',html)
            _write(stage/'dashboard.html',html)
            _write(stage/'robots.txt','User-agent: *\nDisallow: /\n')
            _write(stage/'.nojekyll','')
            hashes={p.relative_to(stage).as_posix():{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size}
                for p in sorted(stage.rglob('*')) if p.is_file()}
            artifact={'format':1,'generated_at':payload['meta']['generated_at'],'files':hashes,
                'stocks':len(payload['items']),'source':payload['meta']['source'],'range':payload['range']}
            _write(stage/'artifact.json',json.dumps(artifact,ensure_ascii=False,indent=2))
            checked=verify_artifact(str(stage))
            if not checked['ok']: raise ValueError(checked['error'])
            if target.exists():
                backup=Path(tempfile.mkdtemp(prefix='.board-previous-',dir=target.parent))
                backup.rmdir()
                os.replace(target,backup)
            try:
                os.replace(stage,target)
            except BaseException:
                if backup is not None: os.replace(backup,target); backup=None
                raise
            if backup is not None: shutil.rmtree(backup)
            return {'ok':True,'site':str(target),'files':checked['files'],'bytes':checked['bytes'],
                'meta':payload['range'],'kline':kstats,'stocks':len(payload['items'])}
        finally:
            if stage.exists(): shutil.rmtree(stage)


# ────────────────────── 站点固定化（2026-09-16 改造） ──────────────────────
# 目标：站点外壳只生成一次；每轮只更新 docs/data/ 下的 JSON，浏览器运行时 fetch。
#
# 旧路径每轮重建整个 docs/（约 39MB）：data_list.js 2.66MB、16 个 K 线分片各 ~1MB，
# 且每个产物还存一份内容哈希副本（体积翻倍）。数据每天只更新一次，
# 没必要连 HTML/JS 一起重写。
#
# 破缓存：不再用内容哈希文件名，改用 Pages 实测的 max-age=600（10 分钟）。

DATA_SUBDIR='data'

# 固定层文件（不含 data/ 子树）
SHELL_FILES=('index.html','dashboard.html','dashboard.js','lib/echarts.min.js','robots.txt','.nojekyll')


def _write_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')),encoding='utf-8')
    return path.stat().st_size


def _safe_shell(site):
    """站点目录校验：只允许写站点目录本身，不允许碰源码与持久数据。"""
    shell=Path(site).absolute()
    if shell.is_symlink(): raise ValueError('站点目录不能是符号链接')
    shell=shell.resolve()
    repo=Path(BASE_DIR).resolve()
    data=Path(data_dir()).resolve()
    if shell==repo or shell in repo.parents or shell==data or shell in data.parents or data in shell.parents:
        raise ValueError('站点目录不能覆盖源码或持久数据')
    for protected in ('app','config','function','scripts','tools','web','tests'):
        p=repo/protected
        if shell==p or p in shell.parents: raise ValueError('站点目录不能位于源码子目录中')
    return shell


def build_shell(site=None):
    """生成固定层。只在改前端（HTML/JS/CSS）时跑一次，日常更新不走这里。"""
    target=_safe_shell(site or site_dir())
    target.mkdir(parents=True,exist_ok=True)
    html=(Path(web_dir())/'dashboard.html').read_text(encoding='utf-8')
    html=re.sub(r'<title>.*?</title>',lambda _: '<title>'+html_lib.escape(site_title())+'</title>',html,count=1)
    html=html.replace('<head>','<head>\n<meta name="robots" content="noindex,nofollow">',1)
    _write(target/'index.html',html)
    _write(target/'dashboard.html',html)
    shutil.copyfile(Path(web_dir())/'dashboard.js',target/'dashboard.js')
    lib_dst=target/'lib'
    if lib_dst.exists(): shutil.rmtree(lib_dst)
    shutil.copytree(Path(web_dir())/'lib',lib_dst)
    _write(target/'robots.txt','User-agent: *\nDisallow: /\n')
    _write(target/'.nojekyll','')
    # ★ 清单必须排除 artifact.json 自己：rglob 扫的是**写入之前**的目录，此刻磁盘上
    #   还躺着上一版的 artifact.json。把它算进 files，等于在清单里记下自己的**过期**
    #   sha256，紧接着 _write_json 又把它覆盖掉 → verify_shell 逐文件核对时必然报
    #   「产物损坏: artifact.json」，于是“改完前端跑一次标准 `cli build-shell`”
    #   会假失败（2026-09-19 发现）。排除之后 files 只含真正的固定层文件，
    #   与 validator._verify_manifest 的 `actual == set(files) | {'artifact.json'}` 判据正好对上。
    manifest_path=target/'artifact.json'
    hashes={p.relative_to(target).as_posix():{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size}
        for p in sorted(target.rglob('*')) if p.is_file() and p!=manifest_path and DATA_SUBDIR not in p.parts}
    _write_json(target/'artifact.json',{'format':2,'kind':'shell',
        'generated_at':board.now_cn().isoformat(),'files':hashes})
    return {'ok':True,'kind':'shell','site':str(target),'files':len(hashes),
        'bytes':sum(v['size'] for v in hashes.values())}


# ────────────────────── 数据层 v2（2026-09-19 改造） ──────────────────────
# 目标：首屏极快 + 切板块/时间按需加载 + 单股详情按需加载。
#
# 产物：
#   meta.json                 元信息 + taxonomy 内联（首屏第 1 个请求）
#   home.json                 首屏档：主板 + 非ST + 近3天（第 2 个请求）
#   list-<key>.json           分板块档：实体 + 窗口内全部公告
#   stock/<前2位>/<code>.json 单股详情：K线 + 全部公告（含 URL）
#
# 条目 = 13 元数组，省掉全部 key 名（全市场约省 750KB）：
#   [c, n, b, st, cat, cap, p, ch, ch5, cha, d, lu, a]
#   a = [[date,title,category_id], ...] —— **不带 URL**（URL 占约 2.4MB，只放单股详情）
#
# ★ K 线不再走 16 分片：点一只股票从 1.5~1.8MB 降到 ~7KB（-99.6%）。
# ★ 未变化的文件用硬链接复用（inode/mtime 不变）→ git 直接跳过，解决"每轮全量重建"的开销。

BOARD_KEYS=(('main','主板'),('gem','创业板'),('star','科创板'),('bse','北交所'))
HOME_RANGE_DAYS=3
ITEM_FIELDS=13


def _cutoff(days):
    """近 N 天窗口的起点（含今天）。"""
    from datetime import timedelta
    from .timeutil import today_cn
    return (today_cn()-timedelta(days=max(1,days)-1)).isoformat()


def _entity(s):
    """实体的 12 元前缀（不含公告数组）。"""
    anns=s.get('announcements') or []
    latest=anns[-1] if anns else {}
    return [s.get('code'),s.get('name'),s.get('board'),1 if s.get('is_st') else 0,
        s.get('category_id'),s.get('market_cap'),s.get('last_close'),s.get('chg'),
        s.get('chg5'),s.get('chg_ann'),s.get('price_date'),latest.get('url') or '']


def _item(s,anns):
    """13 元条目：12 元实体 + 公告三元素数组。"""
    return _entity(s)+[[[a.get('date'),a.get('title'),a.get('category_id')] for a in anns]]


def _kline_map(bars):
    """code -> [[date,open,close,high,low,volume], ...]，最近 bars 根，按日期升序。"""
    out={}
    rows=db.conn().execute('''SELECT code,date,open,close,high,low,volume FROM (
        SELECT *, ROW_NUMBER() OVER(PARTITION BY code ORDER BY date DESC) rn
        FROM current_klines) WHERE rn<=? ORDER BY code,date''',(bars,))
    for row in rows:
        out.setdefault(row['code'],[]).append(
            [row['date'],row['open'],row['close'],row['high'],row['low'],row['volume']])
    return out


def _csv_cell(value):
    text='' if value is None else str(value)
    if any(ch in text for ch in ',"\n\r'):
        return '"'+text.replace('"','""')+'"'
    return text


def _write_ai_csv(repo_root,items,taxonomy):
    """DB-B：一行一个 (股票, 公告类型) 对，供 AI 挑池。

    类别写**中文标签** —— AI 零歧义（personnel / restructure / merger 这些英文 id 太像，
    容易猜错）。只放 code / name / category 三个字段，剩下的 AI 自己去取。
    """
    label={t.get('id'):(t.get('label') or t.get('id')) for t in taxonomy}
    lines=['code,name,category']
    for s in items:
        seen=[]
        for a in (s.get('announcements') or []):
            cid=a.get('category_id')
            if cid and cid not in seen:
                seen.append(cid)
        for cid in seen:
            lines.append(','.join([_csv_cell(s.get('code')),_csv_cell(s.get('name')),
                _csv_cell(label.get(cid,cid))]))
    path=Path(repo_root)/'ai'/'stocks.csv'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return {'rows':len(lines)-1,'bytes':path.stat().st_size,'path':str(path)}


def _write_data_layer(stage,payload,bars,repo_root):
    """写数据层 v2 的全部产物，返回统计。"""
    items=payload.get('items') or []
    taxonomy=payload.get('taxonomy') or []
    meta_in=dict(payload.get('meta') or {})
    rng=payload.get('range') or {}

    # 1) 单股详情（K 线 + 全部公告含 URL）
    klines=_kline_map(bars)
    stock_root=Path(stage)/'stock'
    for s in items:
        code=str(s.get('code') or '')
        if not code: continue
        _write_json(stock_root/code[:2]/(code+'.json'),{
            'v':2,'c':code,'n':s.get('name'),'b':s.get('board'),'st':1 if s.get('is_st') else 0,
            'cat':s.get('category_id'),'cap':s.get('market_cap'),'p':s.get('last_close'),
            'ch':s.get('chg'),'ch5':s.get('chg5'),'cha':s.get('chg_ann'),
            'd':s.get('price_date'),'src':s.get('price_source'),'adj':s.get('adjustment'),
            'k':klines.get(code,[]),
            'a':[[a.get('date'),a.get('title'),a.get('category_id'),a.get('url') or ''
                  ] for a in (s.get('announcements') or [])]})

    # 2) 分板块档（实体 + 窗口内全部公告）
    counts={'stocks':len(items)}
    for key,board_name in BOARD_KEYS:
        subset=[s for s in items if s.get('board')==board_name]
        counts[key]=len(subset)
        _write_json(Path(stage)/f'list-{key}.json',
            {'v':2,'key':key,'count':len(subset),
             'items':[_item(s,s.get('announcements') or []) for s in subset]})
    st_subset=[s for s in items if s.get('is_st')]
    counts['st']=len(st_subset)
    _write_json(Path(stage)/'list-st.json',
        {'v':2,'key':'st','count':len(st_subset),
         'items':[_item(s,s.get('announcements') or []) for s in st_subset]})
    counts['all']=len(items)
    _write_json(Path(stage)/'list-all.json',
        {'v':2,'key':'all','count':len(items),
         'items':[_item(s,s.get('announcements') or []) for s in items]})

    # 3) 首屏档：主板 + 非ST + 近3天（用户 90% 的用法，一个请求直达）
    cut=_cutoff(HOME_RANGE_DAYS)
    home=[]
    for s in items:
        if s.get('board')!='主板' or s.get('is_st'): continue
        anns=[a for a in (s.get('announcements') or []) if (a.get('date') or '')>=cut]
        if not anns: continue
        home.append(_item(s,anns))
    counts['home']=len(home)
    _write_json(Path(stage)/'home.json',
        {'v':2,'key':'home','range_days':HOME_RANGE_DAYS,'count':len(home),'items':home})

    # 4) meta（taxonomy 内联，省掉首屏一个请求）
    _write_json(Path(stage)/'meta.json',{
        'v':2,'generated_at':meta_in.get('generated_at'),'source':meta_in.get('source'),
        'mock':meta_in.get('mock'),'range':rng,
        'latest_announcement':meta_in.get('latest_announcement'),
        'latest_price':meta_in.get('latest_price'),'run':meta_in.get('run'),
        'coverage':meta_in.get('coverage'),'kline_bars':bars,
        'boards':[b for _,b in BOARD_KEYS],'home_range_days':HOME_RANGE_DAYS,
        'counts':counts,'taxonomy':taxonomy})

    return {'counts':counts,'ai':_write_ai_csv(repo_root,items,taxonomy)}


def _reuse_unchanged(old_root,new_root):
    """把 new_root 中与 old_root 内容相同的文件换成硬链接。

    硬链接后 inode 与 mtime 都不变 → `git add -A` 直接跳过这些文件，
    解决"每轮全量重建 5000 个单股文件"的 stat/hash 开销。内容不变的 blob
    本来就不会重复入库（git 按内容寻址），所以仓库也不会因此膨胀。
    """
    old_root=Path(old_root); new_root=Path(new_root)
    reused=0
    for p in new_root.rglob('*'):
        if not p.is_file(): continue
        old=old_root/p.relative_to(new_root)
        try:
            if not old.is_file(): continue
            if old.stat().st_size!=p.stat().st_size: continue
            if old.read_bytes()!=p.read_bytes(): continue
            p.unlink()
            os.link(old,p)
            reused+=1
        except OSError:
            continue
    return reused


def _db_b_root(shell,repo_root=None):
    """解析 DB-B（`ai/stocks.csv`）的落点 = 数据仓根目录。

    ★ 不能默认 `shell.parent` 就完事：`build_data()` 不传 `site=` 时 shell 是
      `<代码仓>/dist`，`shell.parent` 就是**代码仓根** —— 会把给 AI 的 CSV 写进源码树，
      而 cycle 提交的是数据仓，等于白写还污染源码。所以默认值要显式校验，
      且 cycle 必须把 `repo_dir` 传进来。
    """
    shell=Path(shell).resolve()
    root=Path(repo_root).resolve() if repo_root else shell.parent
    code=Path(BASE_DIR).resolve()
    if root==code or root in code.parents or root==shell:
        raise ValueError('DB-B 落点解析到了源码仓，拒绝写入；请显式传 repo_root')
    return root


def build_data(site=None,days=90,repo_root=None):
    """生成数据层 docs/data/*.json。这是每轮 cycle 唯一要跑的建站步骤。

    `repo_root` = 数据仓根目录，DB-B 落在它下面的 `ai/stocks.csv`。
    """
    shell=_safe_shell(site or site_dir())
    shell.mkdir(parents=True,exist_ok=True)
    root=_db_b_root(shell,repo_root)
    target=shell/DATA_SUBDIR
    with writer_lock():
        db.init_db()
        stage=Path(tempfile.mkdtemp(prefix='.board-data-',dir=shell))
        try:
            with db.transaction():
                payload=board.build_payload(days)
                bars=int(get_build_config().get('kline_bars',KLINE_BARS))
                if not 6<=bars<=640: raise ValueError('build.kline_bars 必须在6到640之间')
                stats=_write_data_layer(stage,payload,bars,root)
            files={p.relative_to(stage).as_posix():{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size}
                for p in sorted(stage.rglob('*')) if p.is_file()}
            _write_json(stage/'artifact.json',{'format':2,'kind':'data','schema':2,
                'generated_at':payload['meta']['generated_at'],'files':files,
                'stocks':len(payload['items']),'source':payload['meta']['source'],'range':payload['range']})
            # ★ 安装前自检：stage 校验不过就绝不安装，旧数据层原样保留。
            checked=verify_data_dir(stage)
            if not checked.get('ok'):
                return {'ok':False,'kind':'data','site':str(target),
                    'error':'数据层自检失败: '+str(checked.get('error'))}
            # 未变化的文件换硬链接（必须在自检之后：硬链接只改 inode，不改内容）
            if target.exists():
                stats['reused']=_reuse_unchanged(target,stage)
            backup=None
            if target.exists():
                backup=Path(tempfile.mkdtemp(prefix='.board-data-prev-',dir=shell))
                backup.rmdir()
                os.replace(target,backup)
            try:
                os.replace(stage,target)
            except BaseException:
                if backup is not None: os.replace(backup,target); backup=None
                raise
            if backup is not None: shutil.rmtree(backup)
            return {'ok':True,'kind':'data','site':str(target),'files':len(files),
                'bytes':sum(v['size'] for v in files.values()),'stocks':len(payload['items']),
                'meta':payload['range'],'counts':stats['counts'],'ai':stats['ai'],
                'reused':stats.get('reused',0)}
        finally:
            if stage.exists(): shutil.rmtree(stage)
