"""一次性完整更新：GitHub → 临时 SQLite → 采集 → 校验 → 建站 → 导出 → 提交。

这是本项目的唯一主流程（规划书第十二章的定时流程，第十六章的成功条件）。

★ 核心不变量：任何一步不通过都不提交 GitHub。
  因此 GitHub Pages 上永远保留着上一份可用版本，不存在"更新到一半网站坏了"的中间态。

★ 与旧版 remote_cycle 的一个关键差异：运行状态文件 update.json 在**导出之前**写入。
  旧版是先 export 再 meta_set，导致写进 SQLite 的状态要等下一轮才导出，永远滞后一轮。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import github_store, pipeline, site, store, validator
from .timeutil import now_cn

DEFAULT_CHECK_DAYS = 90


def _version(stamp) -> str:
    return stamp.strftime('%Y%m%d-%H%M%S')


def _write_update(repo_dir: str, payload: dict):
    """原子写 data/meta/update.json。"""
    meta = Path(repo_dir) / 'data' / 'meta'
    meta.mkdir(parents=True, exist_ok=True)
    target = meta / 'update.json'
    tmp = target.with_name('.update.json.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    os.replace(tmp, target)


def run(repo_dir, *, mode='incremental', lookback_days=None, max_attempts=2, retry_delay=10,
        repo=None, token=None, branch='main', days=DEFAULT_CHECK_DAYS, push=True):
    started = now_cn()
    version = _version(started)
    repo_dir = str(Path(repo_dir).resolve())
    repo = repo or os.getenv('BOARD_DATA_REPO', '').strip()
    token = token or os.getenv('GITHUB_TOKEN', '').strip()
    if push and (not repo or not token):
        raise ValueError('缺少 BOARD_DATA_REPO 或 GITHUB_TOKEN')
    Path(repo_dir).mkdir(parents=True, exist_ok=True)

    steps = {}
    # 目录名跟随 github_store.SITE_DIR（docs/）：GitHub Pages 分支发布只认 / 或 /docs。
    site_target = str(Path(repo_dir) / github_store.SITE_DIR)

    if push:
        github_store.clone_or_open(repo, repo_dir, token, branch)
        steps['import'] = github_store.import_data(repo_dir)
    else:
        # 本地/演示模式：没有数据仓库可连，从空库开始，用于离线验证建站链路。
        steps['import'] = {'ok': True, 'skipped': True,
                           'reason': '本地模式（--no-push），未连接 GitHub 数据仓库'}

    update = pipeline.run_update(mode=mode, lookback_days=lookback_days,
                                 max_attempts=max_attempts, retry_delay=retry_delay)
    steps['update'] = update
    if not update.get('ok'):
        return {'ok': False, 'stage': 'update', 'version': version, 'steps': steps,
                'pushed': False, 'error': update.get('error'),
                'reason': '采集未完整成功，保持 GitHub 旧版本'}

    # ★ 站点固定化改造（2026-09-16）：每轮只更新数据层 docs/data/*.json，
    #   不再重建整个站点外壳。HTML/JS/CSS 由 `app.cli build-shell` 一次性生成后固定。
    build = site.build_data(site_target, days)
    steps['build'] = build
    if not build.get('ok'):
        return {'ok': False, 'stage': 'build', 'version': version, 'steps': steps,
                'pushed': False, 'error': build.get('error'),
                'reason': '数据层生成失败，保持 GitHub 旧版本'}

    check = validator.check_data(days)
    steps['check'] = check
    if not check.get('ok'):
        return {'ok': False, 'stage': 'check', 'version': version, 'steps': steps,
                'pushed': False, 'error': check.get('error'),
                'reason': '数据完整性检查未通过，保持 GitHub 旧版本'}

    finished = now_cn()
    payload = {
        'status': 'success',
        'started_at': started.isoformat(timespec='seconds'),
        'finished_at': finished.isoformat(timespec='seconds'),
        'announcement_added': int(update.get('new', 0) or 0),
        'kline_updated': int(update.get('klines', 0) or 0),
        'market_cap_updated': int(update.get('market_caps', 0) or 0),
        'commit': None,
        'version': version,
    }
    store.meta_set('last_version', version)
    store.meta_set('last_update_at', finished.isoformat(timespec='seconds'))
    _write_update(repo_dir, payload)
    steps['export'] = github_store.export_data(repo_dir)

    if not push:
        return {'ok': True, 'stage': 'built', 'version': version, 'steps': steps,
                'pushed': False, 'site': site_target}

    pushed = github_store.commit_push(repo_dir, repo, token,
                                      f'chore(data): update {version}', branch)
    steps['push'] = pushed

    # commit sha 只有推送后才知道，回填后单独提交一次。
    # 这一步失败不影响已上线的版本：数据与站点已经在上面那个提交里了。
    if pushed.get('commit'):
        payload['commit'] = pushed['commit']
        _write_update(repo_dir, payload)
        try:
            github_store.commit_push(repo_dir, repo, token,
                                     f'chore(meta): record commit for {version}', branch)
        except Exception as exc:
            pushed['meta_commit_error'] = f'{type(exc).__name__}: {exc}'

    return {'ok': True, 'stage': 'complete', 'version': version, 'steps': steps,
            'push': pushed, 'site': site_target}
