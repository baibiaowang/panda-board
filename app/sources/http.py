"""
HTTP 客户端：有界连接池、连接/读取超时、响应体上限和有限退避。

默认验证 TLS，不自动退回明文 HTTP。显式 HTTP 代理对 HTTPS 目标使用
CONNECT。池大小默认为8，但吞吐量取决于实际网络与来源限流；旧代码中的
历史测速不能作为当前平台性能保证。腾讯报价按 GBK 解码。
"""
from __future__ import annotations

import base64
import gzip
import io
import http.client
import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

from ..config import get_network_config

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

DEFAULT_POOL_SIZE = 8


class HttpError(RuntimeError):
    pass


class _Status(Exception):
    """非 2xx 响应。单独一个类型，好让上层区分"限流"和"连不上"。"""

    def __init__(self, code: int, retry_after: str = ""):
        super().__init__("HTTP %d" % code)
        self.code = code
        self.retry_after = retry_after


class _HostPool:
    """
    单个 (scheme, host, port) 的连接池，容量有上限。

    ★ 为什么是"有上限的池"而不是"每线程一条长连接"：
      `threading.local()` 那种写法下**连接数 = 线程数**，完全不受控 ——
      上游把 workers 从 8 调到 16，连接数就跟着翻倍，而实测 16 条
      跨洋连接在只有 2 核 CPU 配额的沙箱里会互相抢，耗时反而抖到 43 倍。
      有上限的池能把连接数**钉死在最优点**，不管上游开多少线程。

    借还协议：acquire() 可能阻塞（池满时等别人还），release() 负责唤醒。
    连接不可复用时（对端要关、或中途异常）传 reusable=False，池子会
    关掉它并让出名额。
    """

    def __init__(self, size: int):
        self.size = max(1, int(size))
        self._free: list = []
        self._n = 0                       # 已建连接数（含借出的）
        self._cond = threading.Condition()
        self._closed = False

    def acquire(self, factory, timeout=35):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    raise HttpError("HTTP 连接池已关闭")
                if self._free:
                    return self._free.pop()
                if self._n < self.size:
                    self._n += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HttpError("等待 HTTP 连接池超时")
                self._cond.wait(remaining)
        try:
            return factory()
        except BaseException:
            with self._cond:
                self._n -= 1
                self._cond.notify()
            raise

    def release(self, conn, reusable: bool) -> None:
        with self._cond:
            if reusable and not self._closed:
                self._free.append(conn)
            else:
                self._n -= 1
                _quiet_close(conn)
            self._cond.notify()

    def close_all(self) -> None:
        with self._cond:
            self._closed = True
            for conn in self._free:
                _quiet_close(conn)
            self._n -= len(self._free)
            self._free.clear()
            self._cond.notify_all()


def _quiet_close(conn) -> None:
    try:
        conn.close()
    except Exception:                                        # noqa: BLE001
        pass


class HttpClient:
    def __init__(self, cfg: Optional[dict] = None):
        net = cfg if cfg is not None else get_network_config()
        self.connect_timeout = float(net.get("connect_timeout", 10))
        self.read_timeout = float(net.get("read_timeout", 25))
        self.timeout = self.connect_timeout + self.read_timeout
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("HTTP 超时必须为正数")
        self.max_response_bytes = max(1024, int(net.get("max_response_bytes", 16 * 1024 * 1024)))
        self.allow_http_fallback = bool(net.get("allow_http_fallback", False))
        self.max_attempts = max(1, int(net.get("max_attempts", 3)))
        self.backoff_base = float(net.get("backoff_base", 2.0))
        self.user_agent = net.get("user_agent") or DEFAULT_UA
        self.verify_ssl = bool(net.get("verify_ssl", True))
        self.proxy = self._resolve_proxy(net)
        try:
            self.pool_size = max(1, int(net.get("conn_pool_size", DEFAULT_POOL_SIZE) or 1))
        except (TypeError, ValueError):
            self.pool_size = DEFAULT_POOL_SIZE

        self._pools: dict = {}
        self._pools_lock = threading.Lock()
        self._ctx: Optional[ssl.SSLContext] = None

    @staticmethod
    def _resolve_proxy(net: dict) -> Optional[str]:
        explicit = net.get("proxy")
        if explicit:
            return str(explicit)
        if not net.get("proxy_env_fallback", True):
            return None
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            val = os.getenv(key)
            if val:
                return val
        return None

    # ---------------- 连接与池 ----------------
    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        """None 表示用 http.client 的默认上下文（会校验证书）。"""
        if self.verify_ssl:
            return None
        if self._ctx is None:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ctx = ctx
        return self._ctx

    def _proxy_parts(self) -> tuple:
        raw = self.proxy or ""
        if "://" not in raw:
            raw = "http://" + raw
        u = urllib.parse.urlsplit(raw)
        if u.scheme != "http" or not u.hostname:
            raise HttpError("代理必须是 http://host:port；HTTPS 目标使用 CONNECT 隧道")
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        auth = ""
        if u.username:
            token = "%s:%s" % (urllib.parse.unquote(u.username), urllib.parse.unquote(u.password or ""))
            auth = "Basic " + base64.b64encode(token.encode("utf-8")).decode("ascii")
        return host, port, auth

    def _use_proxy_for(self, host: str) -> bool:
        """老 urllib 的 ProxyHandler 会查 no_proxy，这里保持同样的语义。"""
        if not self.proxy:
            return False
        try:
            if urllib.request.proxy_bypass(host):
                return False
        except Exception:                                    # noqa: BLE001
            pass
        return True

    def _connect(self, scheme: str, host: str, port: int, use_proxy: bool):
        ctx = self._ssl_context()
        if use_proxy:
            phost, pport, auth = self._proxy_parts()
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    phost, pport, timeout=self.connect_timeout, context=ctx)
                # set_tunnel 走 CONNECT，之后的 TLS 是端到端的
                conn.set_tunnel(host, port,
                                headers={"Proxy-Authorization": auth} if auth else None)
                return conn
            return http.client.HTTPConnection(phost, pport, timeout=self.connect_timeout)
        if scheme == "https":
            return http.client.HTTPSConnection(
                host, port, timeout=self.connect_timeout, context=ctx)
        return http.client.HTTPConnection(host, port, timeout=self.connect_timeout)

    def _pool_for(self, scheme: str, host: str, port: int) -> _HostPool:
        key = (scheme, host, port)
        with self._pools_lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = _HostPool(self.pool_size)
                self._pools[key] = pool
            return pool

    # ---------------- 单次请求 ----------------
    def _request(self, url: str, method: str, body: Optional[bytes],
                 headers: dict, encoding: str) -> str:
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "https").lower()
        if scheme not in ("http", "https") or parts.username or parts.password:
            raise HttpError("只支持不带内嵌凭据的 HTTP(S) URL")
        host = parts.hostname
        if not host:
            raise HttpError("URL 缺少主机名：%s" % url)
        port = parts.port or (443 if scheme == "https" else 80)
        path = urllib.parse.urlunsplit(
            ("", "", parts.path or "/", parts.query, ""))
        use_proxy = self._use_proxy_for(host)

        hdrs = dict(headers)
        if use_proxy and scheme == "http":
            # 明文 HTTP 过代理：请求行要写绝对 URI，鉴权走请求头
            path = urllib.parse.urlunsplit(
                (scheme, parts.netloc, parts.path or "/", parts.query, ""))
            _, _, auth = self._proxy_parts()
            if auth:
                hdrs.setdefault("Proxy-Authorization", auth)
        # 不主动要压缩：响应体只有十几 KB，省掉解码逻辑
        hdrs.setdefault("Accept-Encoding", "identity")

        pool = self._pool_for(scheme, host, port)
        conn = pool.acquire(lambda: self._connect(scheme, host, port, use_proxy), self.timeout)
        reusable = False
        try:
            if conn.sock is None:
                conn.connect()
            conn.sock.settimeout(self.read_timeout)
            deadline = time.monotonic() + self.read_timeout
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HttpError("读取 HTTP 响应超时")
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                chunk = resp.read1(min(65536, self.max_response_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise HttpError("HTTP 响应超过大小上限")
                chunks.append(chunk)
            raw = b"".join(chunks)
            # will_close 为真说明对端要关（例如调用方显式传了 Connection: close）
            reusable = not resp.will_close
            if not (200 <= resp.status < 300):
                raise _Status(resp.status, resp.getheader("Retry-After", ""))
            if resp.getheader("Content-Encoding", "").lower() == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                    raw = gz.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise HttpError("解压后的响应超过大小上限")
            return raw.decode(encoding, errors="replace")
        finally:
            pool.release(conn, reusable)

    def _fetch(self, url: str, data: Optional[bytes], headers: dict,
               encoding: str, dual_scheme: bool) -> str:
        method = "POST" if data is not None else "GET"
        schemes = (("https", "http") if self.allow_http_fallback else ("https",)) if dual_scheme else (None,)
        last: Optional[Exception] = None
        for scheme in schemes:
            full = url.format(scheme=scheme) if scheme else url
            for attempt in range(self.max_attempts):
                try:
                    return self._request(full, method, data, headers, encoding)
                except _Status as exc:
                    last = exc
                    if exc.code in (403, 408, 429, 500, 502, 503, 504):
                        if attempt + 1 < self.max_attempts:
                            try:
                                delay = float(exc.retry_after)
                            except ValueError:
                                delay = self.backoff_base * (2 ** attempt)
                            time.sleep(max(0, min(60, delay)))
                        continue
                    break
                except Exception as exc:                     # noqa: BLE001
                    last = exc
                    if attempt + 1 < self.max_attempts:
                        time.sleep(max(0, min(60, self.backoff_base * (2 ** attempt))))
        # Exception text from proxy libraries may contain credentials; do not echo it.
        detail = str(last) if isinstance(last, (_Status, HttpError)) else type(last).__name__
        raise HttpError("请求失败 %s：%s" % (urllib.parse.urlsplit(url.replace('{scheme}', 'https')).hostname, detail))

    # ---------------- 对外 ----------------
    def get(self, url: str, headers: Optional[dict] = None,
            encoding: str = "utf-8", dual_scheme: bool = False) -> str:
        h = {"User-Agent": self.user_agent}
        h.update(headers or {})
        return self._fetch(url, None, h, encoding, dual_scheme)

    def get_json(self, url: str, headers: Optional[dict] = None,
                 dual_scheme: bool = False) -> dict:
        raw = self.get(url, headers, "utf-8", dual_scheme)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpError("响应不是 JSON：%s" % exc) from exc

    def post_form_json(self, url: str, fields: dict, headers: Optional[dict] = None,
                       dual_scheme: bool = False) -> dict:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        h = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
             "Accept": "application/json"}
        h.update(headers or {})
        raw = self._fetch(url, body, {**{"User-Agent": self.user_agent}, **h},
                          "utf-8", dual_scheme)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpError("响应不是 JSON：%s" % exc) from exc

    def close(self) -> None:
        """关掉所有池里的空闲连接。进程退出前调一次即可。"""
        with self._pools_lock:
            pools = list(self._pools.values())
        for pool in pools:
            pool.close_all()
