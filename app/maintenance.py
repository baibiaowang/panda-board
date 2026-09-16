"""SQLite-consistent backups and validated restores (includes uncheckpointed WAL)."""
from pathlib import Path
import os
import sqlite3
import tempfile


def snapshot(source,destination):
    source,destination=Path(source).resolve(),Path(destination).absolute()
    if not source.is_file(): raise ValueError('源数据库不存在')
    if source==destination.resolve(): raise ValueError('备份不能覆盖源数据库')
    if destination.exists(): raise ValueError('备份目标已存在，换一个文件名')
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.board-backup-',suffix='.db',dir=destination.parent)
    os.close(fd)
    try:
        original=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=30)
        copy=sqlite3.connect(tmp)
        try:
            original.backup(copy,pages=256)
            rows=copy.execute('PRAGMA integrity_check').fetchall()
            if rows!=[('ok',)]: raise ValueError('备份数据库完整性检查失败')
            tables={r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'announcements','klines','stocks','runs'}<=tables:
                raise ValueError('文件不是有效的看板数据库')
        finally:
            copy.close();original.close()
        # ★ 必须用可写句柄：Windows 下对 'rb' 打开的句柄调 fsync 会抛
        #   OSError [Errno 9] Bad file descriptor，POSIX 上不报错——
        #   所以这个 bug 只在 Windows 上现形。
        with open(tmp,'r+b') as f: os.fsync(f.fileno())
        os.chmod(tmp,0o600)
        os.replace(tmp,destination)
        return str(destination)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
