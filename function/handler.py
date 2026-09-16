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
DEFAULT_TTL = 3600
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


def run_script(cfg):
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
        'BOARD_DATA_DIR=/workspace/work python3 -m app.cli cycle '
        f'--data-repo /workspace/panda-board-data --repo {_q(cfg["data_repo"])} '
        f'--branch {_q(cfg["data_branch"])} > /workspace/update.log 2>&1',
        'echo "$?" > /workspace/update-exit',
        # 自删前 sync：否则 ext4 延迟分配来不及回写，日志尾部留 NUL 空洞，
        # 看起来像被强杀，实际是正常跑完。
        'sync',
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
    sid = resource_id(created.get('id', ''))

    try:
        api.write_file(sid, '/workspace/github-token', cfg['token'].encode())
        api.write_file(sid, '/workspace/run-update.sh', run_script(cfg).encode())
        # 点火即返回。发完用 pgrep 复核，"发出命令"不等于"跑起来了"。
        api.call('POST', f'/v1/sandboxes/{sid}/exec', {
            'cmd': 'chmod +x /workspace/run-update.sh; cd /; '
                   'setsid nohup bash /workspace/run-update.sh </dev/null >/dev/null 2>&1 & '
                   'sleep 2; pgrep -f run-update.sh >/dev/null && echo launched || echo launch-failed',
            'timeout_seconds': 60,
        }, timeout=90)

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
        try:
            api.call('DELETE', f'/v1/sandboxes/{sid}', timeout=60)
        except Exception as exc:
            # 清理失败不能盖掉任务本身的返回结果。
            print('sandbox cleanup failed:', type(exc).__name__, flush=True)


if __name__ == '__main__':
    try:
        result = handler()
    except Exception as exc:
        result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result.get('ok') else 1)
