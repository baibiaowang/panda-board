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
from .validator import verify_artifact

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
