"""Reentrant, process-wide writer lock. Only protects processes in ONE guest OS."""
from contextlib import contextmanager
import errno
import os
import threading
from .paths import lock_path

_mutex=threading.RLock()
_depth=0


class BusyError(RuntimeError):
    pass


@contextmanager
def writer_lock():
    global _depth
    with _mutex:
        if _depth:
            _depth+=1
            try: yield
            finally: _depth-=1
            return
        fd=os.open(lock_path(),os.O_CREAT|os.O_RDWR,0o600)
        try:
            if os.name=='nt':
                import msvcrt
                if os.fstat(fd).st_size==0: os.write(fd,b'0')
                os.lseek(fd,0,os.SEEK_SET)
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES,errno.EAGAIN):
                raise BusyError('已有任务正在操作数据库或发布，请稍后重试') from exc
            raise
        _depth=1
        try:
            yield
        finally:
            _depth=0
            if os.name=='nt':
                os.lseek(fd,0,os.SEEK_SET)
                msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)
