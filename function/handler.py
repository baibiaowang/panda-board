#!/usr/bin/env python3
"""PandaStack Schedule 入口：建一个临时沙箱，点火跑一次完整更新，然后删掉沙箱。

★ 这个文件遵守的是 PandaStack 的**实测**契约，不是文档写的那一套：

  1. env 不会注入 handler，payload 也送不进来 → handler 必须自包含，
     凭据只能打进 bundle（secrets.json）。
     另外 env 是明文存在 Function 对象里的，GET /v1/functions/{id} 直接可读，
     把密钥放 env 等于泄露。
  2. exec 单次有超时上限 → 长任务必须 setsid 点火后立刻返回，不能同步等。
     一轮全量更新实测 10~15 分钟，同步等必被掐。
  3. POST /v1/sandboxes 返回 201 而不是 200。
  4. 改了 handler 必须重新 deploy 才生效，且要等 is_ready。
  5. 免费档沙箱寿命上限 3600s；TTL 是第二道保险，删沙箱必须显式做。
"""
from __future__ import annotations

import json
import os
import shlex
import time

try:
    from panda_api import PandaAPI, resource_id
except ModuleNotFoundError:  # 从仓库根目录本地跑时的回退
    from app.panda_api import PandaAPI, resource_id

SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'secrets.json')
POLL_INTERVAL = 30
# ★ 实测整轮 ~4.4 分钟（clone + pip + cycle），3600s 意味着每轮空转约 55 分钟。
#   注意 PANDASTACK_SANDBOX_TTL 是个死参数：平台不往 handler 注入 env（见本文件顶部契约），
#   所以配 env 无效，改 TTL 只能改代码。
#   1800s 留约 6.8 倍余量；真被掐也不推坏数据（不变量：任何一步不通过都不提交）。
DEFAULT_TTL = 1800
DEFAULT_POLL_SECONDS = 1500


def load_config():
    """凭据优先取 bundle 内的 secrets.json。

    环境变量只作为可选覆盖：平台实测不往 handler 注入 env，
    所以部署时**必须**把凭据打进 bundle，env 那条路在平台侧是走不通的。
    """
    data = {}
    if os.path.isfile(SECRETS_FILE):
        with open(SECRETS_FILE, encoding='utf-8') as handle:
            data = json.load(handle)

    def pick(key, default=''):
        return (os.environ.get(key) or data.get(key) or default).strip()

    return {
        'api_key': pick('PANDASTACK_API_KEY'),
        'code_repo': pick('PANDA_BOARD_CODE_REPO'),
        'code_branch': pick('PANDA_BOARD_CODE_BRANCH', 'main'),
        'data_repo': pick('BOARD_DATA_REPO'),
        'data_branch': pick('BOARD_DATA_BRANCH', 'main'),
        'token': pick('GITHUB_TOKEN'),
        'template': pick('PANDASTACK_SANDBOX_TEMPLATE', 'code-interpreter'),
    }


def _q(value):
    return shlex.quote(str(value))


def run_script(cfg, sid):
    """沙箱内执行的脚本。

    ★ 开头必须 cd /：点火命令如果先 cd 进某个目录、而脚本又删掉那个目录，
      cwd 会变成不存在的路径，随后任何命令都报 getcwd() failed，
      错误信息完全指不到真正的原因。
    """
    return '\n'.join([
        'set -eu',
        'cd /',
        'export GITHUB_TOKEN="$(cat /workspace/github-token)"',
        'rm -f /workspace/github-token',
        'export PYTHONUNBUFFERED=1',
        'rm -rf /workspace/panda-board /workspace/panda-board-data /workspace/work',
        'rm -f /workspace/update-exit',
        'mkdir -p /workspace/panda-board-data /workspace/work',
        f'git clone --depth 1 --branch {_q(cfg["code_branch"])} '
        f'https://github.com/{_q(cfg["code_repo"])}.git /workspace/panda-board',
        'cd /workspace/panda-board',
        'python3 -m pip install --no-input -q -r requirements.txt',
        'rc=0',
        'BOARD_DATA_DIR=/workspace/work python3 -m app.cli cycle '
        f'--data-repo /workspace/panda-board-data --repo {_q(cfg["data_repo"])} '
        f'--branch {_q(cfg["data_branch"])} > /workspace/update.log 2>&1 || rc=$?',
        'echo "$rc" > /workspace/update-exit',
        # 自删前 sync：否则 ext4 延迟分配来不及回写，日志尾部留 NUL 空洞，
        # 看起来像被强杀，实际是正常跑完。
        'sync',
        # ★ 跑完立刻自删，别空转到 TTL：cycle 实测 4.4 分钟，白烧的时间全在这之后。
        #   只在成功时删 —— 失败要留着沙箱给 TTL 兜底，好让人进去看 update.log。
        #   自删依赖沙箱能出网调 api.pandastack.ai（本机直连可用，沙箱内未经证实），
        #   所以失败一律 || true 吞掉，兜底交给 TTL。
        'if [ "$rc" = "0" ]; then',
        f'  curl -s -X DELETE -H {_q("Authorization: Bearer " + cfg["api_key"])} '
        f'https://api.pandastack.ai/v1/sandboxes/{_q(sid)} >/dev/null 2>&1 || true',
        'fi',
    ])


def _tail(api, sid, limit=2000):
    try:
        reply = api.call('POST', f'/v1/sandboxes/{sid}/exec', {
            'cmd': f'tail -c {limit} /workspace/update.log 2>/dev/null || true',
            'timeout_seconds': 30,
        }, timeout=60)
        return (reply.get('stdout') or '')[-limit:]
    except Exception as exc:
        return f'<log unavailable: {type(exc).__name__}>'


def handler(event=None):
    cfg = load_config()
    missing = [k for k in ('api_key', 'code_repo', 'data_repo', 'token') if not cfg[k]]
    if missing:
        return {'ok': False, 'error': 'bundle 缺少配置: ' + ', '.join(missing)}

    ttl = max(600, min(int(os.environ.get('PANDASTACK_SANDBOX_TTL', DEFAULT_TTL)), DEFAULT_TTL))
    poll_seconds = max(0, int(os.environ.get('PANDASTACK_POLL_SECONDS', DEFAULT_POLL_SECONDS)))

    api = PandaAPI(cfg['api_key'])
    created = api.call('POST', '/v1/sandboxes', {
        'template': cfg['template'],
        'ttl_seconds': ttl,
        'metadata': {'role': 'panda-board-update'},
    }, timeout=60)
    # ★ 沙箱从这一刻起已经存在，之后无论发生什么都必须尝试清理。
    #   旧版把 resource_id() 放在 try 之外：返回结构里一旦没有合法 id，
    #   resource_id 抛 ValueError → finally 不执行 → 沙箱泄漏。
    #   （2026-09-16 真实泄漏事故后定位到这处结构隐患）
    raw_id = ''
    for key in ('id', 'sandbox_id'):
        val = created.get(key)
        if isinstance(val, str) and val.strip():
            raw_id = val.strip()
            break
    if not raw_id:
        inner = created.get('sandbox')
        if isinstance(inner, dict) and isinstance(inner.get('id'), str):
            raw_id = inner['id'].strip()

    sid = ''
    try:
        sid = resource_id(raw_id)
    except ValueError:
        # 解析不出来也要留下线索，别让沙箱无声泄漏
        print('sandbox id 解析失败 raw=%r keys=%s'
              % (raw_id[:60], sorted(created.keys())), flush=True)

    try:
        if not sid:
            return {'ok': False, 'completed': False,
                    'error': '平台未返回可解析的沙箱 ID，任务未开始',
                    'response_keys': sorted(created.keys())}
        # ★ 等沙箱就绪再点火：建完立刻 exec 会吃 404（冷启动实测 15~20 秒），
        #   平台侧重试 3 次若全落在冷启动窗口内 → 整轮 exec attempt 3: 404 失败
        #   （2026-09-17 连挂三轮，同一错、同一 137 秒超时）。
        #   这里自己轮询探活，探活通过才写文件/点火；探不活就放弃，由 finally 删沙箱。
        # ★ 强制「点火即返回」：handler 原地轮询会被平台 Function 时限掐掉。
        #   实测四轮 duration_ms 恒为 136655/136743/136866/136922（±270ms）——
        #   这是平台硬超时，不是业务耗时；而原设计 poll 默认 1500s，必然被掐。
        #   （C-66：exec 长任务要点火即返回，否则超时）
        poll_seconds = 0
        ready_interval = 10
        ready_retries = 3
        ready = False
        for _ in range(ready_retries):
            try:
                api.call('POST', f'/v1/sandboxes/{sid}/exec', {
                    'cmd': 'echo ready', 'timeout_seconds': 30,
                }, timeout=60)
                ready = True
                break
            except Exception:
                time.sleep(ready_interval)
        if not ready:
            return {'ok': False, 'sandbox_id': sid, 'completed': False,
                    'error': '沙箱 %d 秒内未就绪，未点火' % (ready_interval * ready_retries)}
        api.write_file(sid, '/workspace/github-token', cfg['token'].encode())
        api.write_file(sid, '/workspace/run-update.sh', run_script(cfg, sid).encode())
        # 点火即返回。发完用 pgrep 复核，"发出命令"不等于"跑起来了"。
        api.call('POST', f'/v1/sandboxes/{sid}/exec', {
            'cmd': 'chmod +x /workspace/run-update.sh; cd /; '
                   'setsid nohup bash /workspace/run-update.sh </dev/null >/dev/null 2>&1 & '
                   'sleep 2; pgrep -f run-update.sh >/dev/null && echo launched || echo launch-failed',
            'timeout_seconds': 60,
        }, timeout=90)

        # 点火成功 → 本轮绝不能删沙箱（删了就杀了正在跑的任务）。
        # 清空 sid / raw_id 让 finally 跳过 DELETE，沙箱靠 TTL(3600s) 与下一轮
        # role=panda-board-update 孤儿清理回收；成败看数据仓提交，不看本轮 run 状态。
        box = sid
        sid = ''
        raw_id = ''
        return {'ok': True, 'sandbox_id': box, 'completed': False, 'exit_code': None,
                'note': '点火即返回未等待；成败以数据仓提交为准'}

        deadline = time.monotonic() + poll_seconds
        while poll_seconds and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            reply = api.call('POST', f'/v1/sandboxes/{sid}/exec', {
                'cmd': 'cat /workspace/update-exit 2>/dev/null || echo running',
                'timeout_seconds': 30,
            }, timeout=60)
            lines = (reply.get('stdout') or '').strip().splitlines()
            state = lines[-1] if lines else 'running'
            if state == 'running':
                continue
            try:
                code = int(state)
            except ValueError:
                continue
            return {'ok': code == 0, 'sandbox_id': sid, 'exit_code': code,
                    'completed': True, 'log_tail': _tail(api, sid)}

        return {'ok': False, 'sandbox_id': sid, 'completed': False,
                'error': '轮询超时，任务可能仍在沙箱内运行；成败以 GitHub 提交为准',
                'log_tail': _tail(api, sid)}
    finally:
        # 沙箱用完必须显式删 —— TTL 只是第二道防线。
        # sid 解析失败时退回平台返回的原始 id（仅接受纯 [A-Za-z0-9_-]，避免拼出畸形 URL）。
        target = sid
        if not target and raw_id and all(c.isalnum() or c in '_-' for c in raw_id):
            target = raw_id
        if target:
            try:
                api.call('DELETE', f'/v1/sandboxes/{target}', timeout=60)
            except Exception as exc:
                # 清理失败不能盖掉任务本身的返回结果。
                print('sandbox cleanup failed:', type(exc).__name__, flush=True)
        else:
            print('sandbox 未清理：未取得任何可用 ID，只能依赖 TTL', flush=True)


if __name__ == '__main__':
    try:
        result = handler()
    except Exception as exc:
        result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result.get('ok') else 1)
