#!/usr/bin/env python3
"""Build the PandaStack Function bundle for manual/console deployment.

★ 契约（2026-09-19 实测修正）：平台**不往 handler 注入 env**，payload 也送不进来，
  所以凭据必须打进 bundle 内的 `secrets.json`。
  旧版只打 handler.py + panda_api.py，产出的包会让 handler 在 load_config()
  处变砖、而且**不报错**；重新部署时必须手工再塞一次 secrets.json ——
  这个手工步骤没人知道，等于埋雷（交接文档 §8.2 记了坑，但没改代码）。

  现在改成显式参数：`--secrets PATH` 指一个**仓库外**的凭据文件（推荐，
  这样仓库目录里根本不存在凭据，连 .gitignore 都不需要兜底）。

★ 必须平铺打包：带顶层目录会报 `can't open file '/fn/handler.py'`。
"""
from __future__ import annotations
import argparse, io, json, tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS_NAME = 'secrets.json'


def build_bundle(secrets_path=None):
    members = [('handler.py', ROOT / 'function/handler.py'),
               ('panda_api.py', ROOT / 'app/panda_api.py')]
    if secrets_path is not None:
        members.append((SECRETS_NAME, Path(secrets_path)))
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as archive:
        for name, path in members:
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue(), [name for name, _ in members]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--bundle-only', required=True, metavar='PATH')
    ap.add_argument('--secrets', metavar='PATH', default=None,
                    help='凭据文件路径（建议放仓库外）。不给则打出不含凭据的包，'
                         'handler 会在 load_config() 处变砖。')
    args = ap.parse_args(argv)
    path = Path(args.bundle_only)
    if path.exists():
        raise SystemExit('目标文件已存在；请换一个路径')
    if args.secrets is not None and not Path(args.secrets).is_file():
        raise SystemExit('凭据文件不存在: %s' % args.secrets)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob, members = build_bundle(args.secrets)
    path.write_bytes(blob)
    print(json.dumps({'ok': True, 'bundle': str(path.resolve()),
                      'members': members,
                      'contains_credentials': args.secrets is not None},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
