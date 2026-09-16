#!/usr/bin/env python3
"""Build the PandaStack Function bundle for manual/console deployment.

The current public PandaStack documentation confirms the sandbox REST API, but the
hosted Function deployment/scheduling surface is not documented here as a stable
public REST contract. Therefore this script only builds the secret-free bundle;
Function/Schedule creation is configured in PandaStack's current console/API UI.
"""
from __future__ import annotations
import argparse, io, json, tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def build_bundle():
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as archive:
        for name,path in [('handler.py',ROOT/'function/handler.py'),('panda_api.py',ROOT/'app/panda_api.py')]:
            data=path.read_bytes(); info=tarfile.TarInfo(name); info.size=len(data); info.mode=0o644; info.mtime=0
            archive.addfile(info,io.BytesIO(data))
    return out.getvalue()


def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--bundle-only',required=True,metavar='PATH')
    args=ap.parse_args(argv)
    path=Path(args.bundle_only)
    if path.exists(): raise SystemExit('目标文件已存在；请换一个路径')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(build_bundle())
    print(json.dumps({'ok':True,'bundle':str(path.resolve()),'contains_credentials':False},ensure_ascii=False))

if __name__=='__main__': main()
