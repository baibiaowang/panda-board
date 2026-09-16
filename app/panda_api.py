"""Small PandaStack REST client shared by deployment and maintenance tools."""
import json
import os
from urllib import request,error,parse


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Secret-bearing API calls must not forward Authorization to another origin.
        return None


class PlatformError(RuntimeError):
    def __init__(self,message,status=0):
        super().__init__(message)
        self.status=status


def resource_id(value):
    import re
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',value):
        raise ValueError('无效的平台资源 ID')
    return value


class PandaAPI:
    def __init__(self,key=None,base=None):
        self.key=(key if key is not None else os.getenv('PANDASTACK_API_KEY','')).strip()
        self.base=(base or os.getenv('PANDASTACK_API_URL') or 'https://api.pandastack.ai').rstrip('/')
        if not self.key or self.key.startswith('__'):
            raise ValueError('缺少有效 PANDASTACK_API_KEY 环境变量')
        u=parse.urlsplit(self.base)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('平台 API 地址必须是无内嵌凭据的 HTTPS 地址')
        self._opener=request.build_opener(_NoRedirect())

    def call(self,method,path,body=None,*,timeout=60,raw=None,content_type=None,binary=False):
        if not path.startswith('/v1/'):
            raise ValueError('平台 API 路径必须带 /v1/ 前缀')
        data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers={'Authorization':'Bearer '+self.key,'User-Agent':'curl/8.5.0 panda-board/4.0'}
        if data is not None: headers['Content-Type']=content_type or 'application/json'
        req=request.Request(self.base+path,data=data,headers=headers,method=method)
        try:
            with self._opener.open(req,timeout=timeout) as response:
                blob=response.read(16*1024*1024+1)
                if len(blob)>16*1024*1024:
                    raise PlatformError('平台 JSON 响应过大，停止处理')
                if binary: return blob
                try: return json.loads(blob) if blob.strip() else {}
                except ValueError as exc: raise PlatformError('平台返回非 JSON 响应',response.status) from exc
        except error.HTTPError as exc:
            # Do not print API bodies: some endpoints include environment secrets.
            raise PlatformError(f'PandaStack {method} {path.split("?")[0]} 返回 HTTP {exc.code}',exc.code) from None
        except (error.URLError,TimeoutError) as exc:
            raise PlatformError(f'PandaStack 请求未确认完成: {type(exc).__name__}') from None

    def write_file(self, sandbox_id, path, content, timeout=60):
        """Write bytes to a disposable sandbox filesystem path."""
        import urllib.parse
        resource_id(sandbox_id)
        if not isinstance(path, str) or not path.startswith('/'):
            raise ValueError('沙箱文件路径必须是绝对路径')
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError('沙箱文件内容必须是 bytes')
        req_path = f"/v1/sandboxes/{sandbox_id}/fs?path=" + urllib.parse.quote(path, safe='')
        req = request.Request(self.base + req_path, data=bytes(content),
            headers={'Authorization':'Bearer '+self.key, 'User-Agent':'panda-board/5.0',
                     'Content-Type':'application/octet-stream'}, method='PUT')
        try:
            with self._opener.open(req, timeout=timeout) as response:
                blob=response.read(1024*1024)
                try: return json.loads(blob) if blob.strip() else {}
                except ValueError: return {'bytes':len(content)}
        except error.HTTPError as exc:
            raise PlatformError(f'PandaStack PUT filesystem 返回 HTTP {exc.code}',exc.code) from None
        except (error.URLError,TimeoutError) as exc:
            raise PlatformError(f'PandaStack 文件写入未确认完成: {type(exc).__name__}') from None

    def download(self,path,destination,timeout=180):
        """Stream potentially large backups without buffering them in RAM."""
        if not path.startswith('/v1/'):
            raise ValueError('平台 API 路径必须带 /v1/ 前缀')
        if os.path.exists(destination):
            raise ValueError('下载目标已存在，拒绝覆盖')
        req=request.Request(self.base+path,headers={'Authorization':'Bearer '+self.key,'User-Agent':'curl/8.5.0 panda-board/4.0'})
        import shutil
        import tempfile
        fd,temp=tempfile.mkstemp(prefix='.panda-download-',dir=os.path.dirname(os.path.abspath(destination)))
        try:
            with os.fdopen(fd,'wb') as f:
                with self._opener.open(req,timeout=timeout) as response:
                    shutil.copyfileobj(response,f,1024*1024)
                    f.flush();os.fsync(f.fileno())
            # Atomic no-clobber install; temp and destination are on the same filesystem.
            os.link(temp,destination)
        except Exception:
            raise PlatformError('备份下载失败，未保留不完整文件') from None
        finally:
            os.unlink(temp)
