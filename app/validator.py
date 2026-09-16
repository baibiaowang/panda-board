"""发布前校验：数据完整性 + 静态产物完整性。

规划书第十六章的成功条件是"全部通过才认为本次更新成功"。这里的每一个检查
都要做到 fail-closed：拿不到证据就判失败，不让"看起来没问题"的版本被推上 Pages。

注意校验顺序 —— 先查数据，再查产物。数据有问题时连产物都不必生成。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from . import board, db

REQUIRED_FILES = ('index.html', 'dashboard.html', 'data_list.js',
                  'data_kline_manifest.js', 'lib/echarts.min.js')
KLINE_SHARDS = 16
# 公告数低于这个值说明采集基本没干活，不是"今天确实没公告"能解释的。
MIN_ANNOUNCEMENTS = 50


def verify_artifact(site):
    """逐文件核对 sha256 与大小，并解析产物里的数据是否可加载。

    这是 push 之前的最后一道门。它过的版本，浏览器打开一定能渲染。
    """
    root = Path(site)
    try:
        manifest = json.loads((root / 'artifact.json').read_text(encoding='utf-8'))
        if manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
            raise ValueError('产物校验清单无效')
        files = manifest['files']
        if any(name not in files for name in REQUIRED_FILES):
            raise ValueError('产物缺少必需文件')
        if root.is_symlink() or any(p.is_symlink() for p in root.rglob('*')):
            raise ValueError('产物不能包含符号链接')
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
        if actual != set(files) | {'artifact.json'}:
            raise ValueError('产物文件列表不匹配')
        for name, expected in files.items():
            p = root / name
            if p.resolve().is_relative_to(root.resolve()) is False:
                raise ValueError('产物路径越界')
            if p.stat().st_size != expected['size'] or \
                    hashlib.sha256(p.read_bytes()).hexdigest() != expected['sha256']:
                raise ValueError(f'产物损坏: {name}')
        data = (root / 'data_list.js').read_text(encoding='utf-8')
        prefix = 'window.ANNO_LIST = '
        if not data.startswith(prefix):
            raise ValueError('公告数据格式无效')
        items, _ = json.JSONDecoder().raw_decode(data[len(prefix):])
        if not isinstance(items, list) or len(items) != manifest['stocks']:
            raise ValueError('公告股票数量不匹配')
        km = json.loads((root / 'data_kline_manifest.js').read_text(encoding='utf-8').split('=', 1)[1].strip().removesuffix(';'))
        if km.get('shards') != KLINE_SHARDS or len(km.get('files', {})) != KLINE_SHARDS:
            raise ValueError('K线分片清单无效')
        for n in range(KLINE_SHARDS):
            name = km['files'].get(str(n))
            if name not in files:
                raise ValueError('缺少K线分片')
            text = (root / name).read_text(encoding='utf-8')
            prefix = f'window.ANNO_KLINE_SHARD_{n} = '
            if not text.startswith(prefix) or not isinstance(json.loads(text[len(prefix):].removesuffix(';')), dict):
                raise ValueError('K线分片格式无效')
        if (root / 'index.html').stat().st_size < 2000:
            raise ValueError('首页异常短，可能构建失败')
        return {'ok': True, 'files': len(actual),
                'bytes': sum(p.stat().st_size for p in root.rglob('*') if p.is_file())}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'ok': False, 'error': str(exc)}


def check_data(days=90):
    """数据库层面的完整性检查。返回逐项结果，不抛异常。"""
    db.init_db()
    checks = []

    def add(name, ok, detail=''):
        checks.append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:300]})

    # 离线/演示模式的数据量天然远小于真实全市场，用同一套阈值会把演示判成故障。
    from .sources import source_name
    src = source_name()
    is_mock = src == 'mock'
    total = db.scalar('SELECT COUNT(*) FROM announcements', default=0) or 0
    add('announcements_present', total >= (1 if is_mock else MIN_ANNOUNCEMENTS),
        f'{total} 条' + ('（演示模式）' if is_mock else ''))

    dup = db.scalar('SELECT COUNT(*) FROM (SELECT ann_id FROM announcements GROUP BY ann_id HAVING COUNT(*)>1)', default=0) or 0
    add('announcement_unique', dup == 0, f'{dup} 个重复 ann_id')

    bad_kline = db.scalar(
        'SELECT COUNT(*) FROM klines WHERE open IS NULL OR close IS NULL OR close<=0 OR high<low', default=0) or 0
    add('kline_values_sane', bad_kline == 0, f'{bad_kline} 条异常K线')

    kline_total = db.scalar('SELECT COUNT(*) FROM klines', default=0) or 0
    add('klines_present', kline_total > 0, f'{kline_total} 条')

    cap_date = db.scalar('SELECT MAX(date) FROM market_caps')
    cap_count = db.scalar('SELECT COUNT(*) FROM market_caps WHERE date=?', (cap_date,), default=0) or 0
    # 演示/离线模式没有真实行情源可取市值，缺市值属于预期，不算故障。
    if is_mock:
        add('market_caps_present', True, '演示模式，跳过市值检查')
    else:
        add('market_caps_present', cap_count > 0, f'{cap_count} 条于 {cap_date or "无"}')

    failed_sync = db.scalar("SELECT COUNT(*) FROM kline_sync WHERE status='failed'", default=0) or 0
    synced = db.scalar('SELECT COUNT(*) FROM kline_sync', default=0) or 0
    ratio = (failed_sync / synced) if synced else 0.0
    add('kline_failure_ratio', ratio <= 0.2, f'{failed_sync}/{synced} 只失败')

    # ★ 覆盖窗口必须取"已经开始采集的区间"，不能用固定的 90 天。
    #   采集是渐进的（每轮只补十来天），固定窗口会让第一轮必然判失败、
    #   于是永远不提交、网站永远停在初始状态——这是能静默毁掉整个项目的设计陷阱。
    from .timeutil import today_cn
    earliest = db.scalar('SELECT MIN(date) FROM fetch_days_v2 WHERE source=?', (src,))
    if not earliest:
        coverage, span = {'missing_days': [], 'incomplete_days': []}, 0
    else:
        span = (today_cn() - date.fromisoformat(earliest)).days + 1
        coverage = board.detect_gaps(max(1, min(days, span)))
    gaps = len(coverage.get('missing_days', [])) + len(coverage.get('incomplete_days', []))
    add('recent_coverage', gaps == 0,
        f'{gaps} 个待补日期（覆盖跨度 {span} 天，自 {earliest or "无"}）')

    failed = [c for c in checks if not c['ok']]
    return {'ok': not failed, 'checks': checks,
            'error': '; '.join(f"{c['name']}: {c['detail']}" for c in failed) or None}


# ────────────────────── 站点固定化校验（2026-09-16 改造） ──────────────────────
# 新架构把产物拆成两层，各自独立一份 artifact.json（format=2，用 kind 区分）：
#   · 固定层（站点根）：index.html / dashboard.html / dashboard.js / lib/echarts.min.js
#                       / robots.txt / .nojekyll                —— 只在改前端时重建
#   · 数据层（站点根/data）：stocks.json / taxonomy.json / meta.json
#                       / kline_manifest.json / kline_0..15.json —— 每轮 cycle 重建
# 两层互不覆盖。上面那份旧的 verify_artifact() 要求"目录内容恰好等于清单"，
# 在新架构下必然不成立，但它仍是旧 build() 的守门人，故保留不动。

DATA_SUBDIR = 'data'
SHELL_REQUIRED = ('index.html', 'dashboard.html', 'dashboard.js',
                  'lib/echarts.min.js', 'robots.txt', '.nojekyll')
DATA_REQUIRED = ('stocks.json', 'taxonomy.json', 'meta.json', 'kline_manifest.json')


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _iter_tree(root, exclude=()):
    """遍历 root 下所有条目，跳过 exclude 里的一级子目录（固定层要跳过 data/）。"""
    for p in root.rglob('*'):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in exclude:
            continue
        yield p, rel


def _verify_manifest(root, kind, required, exclude=()):
    """两层共用的清单核对：格式 → 必需文件 → 符号链接 → 文件全集 → 逐文件 sha256。"""
    manifest = _read_json(root / 'artifact.json')
    files = manifest.get('files')
    if manifest.get('format') != 2 or manifest.get('kind') != kind or not isinstance(files, dict):
        raise ValueError(f'{kind} 产物清单无效')
    missing = [n for n in required if n not in files]
    if missing:
        raise ValueError(f'{kind} 产物缺少必需文件: {",".join(missing)}')
    if root.is_symlink() or any(p.is_symlink() for p, _ in _iter_tree(root, exclude)):
        raise ValueError('产物不能包含符号链接')
    actual = {rel.as_posix() for p, rel in _iter_tree(root, exclude) if p.is_file()}
    if actual != set(files) | {'artifact.json'}:
        raise ValueError(f'{kind} 产物文件列表不匹配')
    for name, expected in files.items():
        p = root / name
        if not p.resolve().is_relative_to(root.resolve()):
            raise ValueError('产物路径越界')
        if p.stat().st_size != expected['size'] or \
                hashlib.sha256(p.read_bytes()).hexdigest() != expected['sha256']:
            raise ValueError(f'产物损坏: {name}')
    return manifest, files, actual


def verify_shell(site):
    """校验固定层。只在跑过 build-shell 之后用一次，日常 cycle 不碰它。"""
    root = Path(site)
    try:
        _manifest, files, actual = _verify_manifest(root, 'shell', SHELL_REQUIRED,
                                                    exclude=(DATA_SUBDIR,))
        index, dash = root / 'index.html', root / 'dashboard.html'
        if index.stat().st_size < 2000:
            raise ValueError('首页异常短，可能构建失败')
        if index.read_bytes() != dash.read_bytes():
            raise ValueError('index.html 与 dashboard.html 不一致')
        return {'ok': True, 'kind': 'shell', 'files': len(actual),
                'bytes': sum(v['size'] for v in files.values())}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'ok': False, 'kind': 'shell', 'error': str(exc)}


def verify_data(site):
    """校验数据层 docs/data/。这是每轮 push 之前的最后一道门。"""
    root = Path(site) / DATA_SUBDIR
    try:
        manifest, files, actual = _verify_manifest(root, 'data', DATA_REQUIRED)

        stocks = _read_json(root / 'stocks.json')
        if not isinstance(stocks, list) or not stocks:
            raise ValueError('公告数据为空或格式无效')
        if len(stocks) != manifest['stocks']:
            raise ValueError('公告股票数量不匹配')

        meta = _read_json(root / 'meta.json')
        if not isinstance(meta, dict) or not meta.get('generated_at') or not meta.get('source'):
            raise ValueError('meta.json 缺少 generated_at/source')
        if meta.get('kline_shards') != KLINE_SHARDS:
            raise ValueError('meta.json 的 kline_shards 与常量不一致')

        km = _read_json(root / 'kline_manifest.json')
        if not isinstance(km, dict) or km.get('shards') != KLINE_SHARDS \
                or len(km.get('files', {})) != KLINE_SHARDS:
            raise ValueError('K线分片清单无效')
        bars, codes = km.get('bars'), 0
        for n in range(KLINE_SHARDS):
            name = km['files'].get(str(n))
            if name not in files:
                raise ValueError('缺少K线分片')
            bucket = _read_json(root / name)
            if not isinstance(bucket, dict):
                raise ValueError('K线分片格式无效')
            for series in bucket.values():
                if not isinstance(series, list) or (bars and len(series) > bars):
                    raise ValueError('K线分片格式无效')
            codes += len(bucket)
        if codes != km.get('codes'):
            raise ValueError('K线代码数与清单不符')

        return {'ok': True, 'kind': 'data', 'files': len(actual),
                'bytes': sum(v['size'] for v in files.values()),
                'stocks': len(stocks), 'codes': codes}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'ok': False, 'kind': 'data', 'error': str(exc)}
