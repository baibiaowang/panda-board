"""Read literal KEY=value configuration without executing shell content."""
import os
import re
import shlex
import stat
from pathlib import Path


def load_env(path):
    path=Path(path)
    if not path.is_file(): raise ValueError(f'缺少配置文件: {path}')
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError('配置含密钥，文件权限必须为 600 或更严格')
    values={}
    for i,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
        line=line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('export '): line=line[7:].strip()
        key,sep,value=line.partition('=')
        key=key.strip()
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*',key): raise ValueError(f'配置第{i}行格式错误')
        if not (key.startswith(('BOARD_','PANDASTACK_')) or key in ('GITHUB_TOKEN','HTTPS_PROXY','HTTP_PROXY','NO_PROXY')):
            raise ValueError(f'配置变量不受支持: {key}')
        parts=shlex.split(value,comments=True,posix=True)
        if len(parts)>1: raise ValueError(f'配置第{i}行含未加引号的空格')
        values[key]=parts[0] if parts else ''
    os.environ.update(values)
    return values
