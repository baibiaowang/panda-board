#!/usr/bin/env python3
"""
HttpClient 连接池的验证脚本（正确性 + 性能对照）。

用法：
    python3 tools/test_http_pool.py              # 只跑正确性（本地起 HTTP 服务，无需外网）
    python3 tools/test_http_pool.py --live       # 再加真网连通性 + 性能对照
    python3 tools/test_http_pool.py --live --proxy http://192.168.3.110:1088

为什么要有这个脚本：
  `app/sources/http.py` 从 urllib（每次新建连接）换成了有上限的连接池。
  这类改动最容易悄悄坏掉的三件事 —— 连接不回收、并发把池冲爆、
  拿到死连接不重试 —— 都**不会让主流程报错**，只会让抓取变慢或静默丢数据。
  所以必须有能自动判定的测试，而不是靠"跑一遍看着没报错"。

正确性用例全部打**本地起的一个 HTTP 服务**，不碰外网：
  这样既不受沙箱出口抖动影响，也能构造 /404、/403、Connection: close
  这些真网里很难复现的边界。真网只用来量加速比。
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sources.http import HttpClient, HttpError        # noqa: E402

LOCAL_CFG = {
    "connect_timeout": 5,
    "read_timeout": 10,
    "max_attempts": 3,
    "backoff_base": 0.02,          # 测试里退避要快，别真的睡 2 秒
    "proxy": None,
    "proxy_env_fallback": False,   # 本地服务绝不能被代理绕走
    "verify_ssl": True,
    "conn_pool_size": 8,
}

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print("  %s %-46s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


HITS: dict = {}          # path -> 命中次数，用来断言"重试了几次"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # 静音
        pass

    def do_GET(self):
        HITS[self.path] = HITS.get(self.path, 0) + 1
        if self.path == "/404":
            return self._send(404, b"nope")
        if self.path == "/403":
            return self._send(403, b"slow down")
        if self.path == "/close":
            return self._send(200, b"bye", close=True)
        return self._send(200, b"ok")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        # 回显解析后的表单（扁平化），方便断言"服务端真的收到了这些字段"
        qs = urllib.parse.parse_qs(body.decode("utf-8"))
        flat = {k: v[0] for k, v in qs.items()}
        return self._send(200, json.dumps(flat, ensure_ascii=False).encode("utf-8"))

    def _send(self, code: int, body: bytes, close: bool = False):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class CountingHttpClient(HttpClient):
    """记下到底建了几条连接 —— 池化改动的核心断言全靠它。"""

    def __init__(self, *a, **kw):
        self.created: list = []
        self._clk = threading.Lock()
        super().__init__(*a, **kw)

    def _connect(self, scheme, host, port, use_proxy):
        conn = super()._connect(scheme, host, port, use_proxy)
        with self._clk:
            self.created.append((scheme, host, port))
        return conn

    @property
    def n_conn(self) -> int:
        with self._clk:
            return len(self.created)


def start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------- 正确性
def test_correctness():
    srv, port = start_server()
    base = "http://127.0.0.1:%d" % port
    print("\n[1] 正确性（本地 HTTP 服务，端口 %d）" % port, flush=True)

    c = CountingHttpClient(LOCAL_CFG)

    # 1. 基本 GET
    ok = c.get(base + "/get") == "ok"
    check("GET 返回内容正确", ok)

    # 2. ★ 复用：5 次请求只能建 1 条连接
    c2 = CountingHttpClient(LOCAL_CFG)
    for _ in range(5):
        c2.get(base + "/get")
    check("5 次串行 GET 只建 1 条连接", c2.n_conn == 1, "实际建了 %d 条" % c2.n_conn)

    # 3. ★ 池上限：16 线程同时借，池大小 8 → 建连接数必须 <= 8
    c3 = CountingHttpClient(LOCAL_CFG)
    errs: list = []

    def hit():
        try:
            c3.get(base + "/get")
        except Exception as exc:                             # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=hit) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("16 线程并发不报错", not errs, str(errs[:1]))
    check("16 线程并发连接数被压在 8 以内",
          c3.n_conn <= LOCAL_CFG["conn_pool_size"], "实际建了 %d 条" % c3.n_conn)
    check("并发后池子被复用（连接数 < 线程数）",
          c3.n_conn < 16, "实际建了 %d 条" % c3.n_conn)

    # 4. POST 表单（巨潮走的就是这条路）
    got = c.post_form_json(base + "/post", {"a": "1", "b": "中文"})
    check("POST 表单往返正确", got == {"a": "1", "b": "中文"}, str(got))

    # 5. ★ Connection: close 的响应不能被塞回池子
    c5 = CountingHttpClient(LOCAL_CFG)
    c5.get(base + "/close", {"Connection": "close"})
    before = c5.n_conn
    c5.get(base + "/get")
    check("对端 Connection: close 后不复用死连接",
          c5.n_conn == before + 1, "建连接数 %d → %d" % (before, c5.n_conn))

    # 6. 404 必须抛 HttpError，不能静默返回空；且非 403 不该白重试
    HITS.pop("/404", None)
    c6 = CountingHttpClient(LOCAL_CFG)
    try:
        c6.get(base + "/404")
        check("404 抛 HttpError", False, "居然没抛")
    except HttpError as exc:
        check("404 抛 HttpError", "404" in str(exc), str(exc)[:60])
    check("404 只请求 1 次（非 403 不重试）",
          HITS.get("/404") == 1, "实际请求 %d 次" % HITS.get("/404", 0))

    # 7. ★ 403 要退避重试满 max_attempts 次（巨潮限流靠这条）
    HITS.pop("/403", None)
    c7 = CountingHttpClient(LOCAL_CFG)
    try:
        c7.get(base + "/403")
        check("403 最终抛 HttpError", False, "居然没抛")
    except HttpError:
        check("403 最终抛 HttpError", True)
    check("403 退避重试满 3 次",
          HITS.get("/403") == LOCAL_CFG["max_attempts"],
          "实际请求 %d 次（期望 %d）" % (HITS.get("/403", 0), LOCAL_CFG["max_attempts"]))

    # 8a. 池里那条连接对象被 close() → http.client 自己会重连，请求仍成功
    c8 = CountingHttpClient(LOCAL_CFG)
    c8.get(base + "/get")
    pool8 = c8._pool_for("http", "127.0.0.1", port)
    for conn in list(pool8._free):
        conn.close()
    try:
        check("池里连接被 close() 后请求仍成功",
              c8.get(base + "/get") == "ok")
    except Exception as exc:                                 # noqa: BLE001
        check("池里连接被 close() 后请求仍成功", False, repr(exc))

    # 8b. ★★ 真正危险的那种：对端悄悄关了 TCP，本地 socket 对象还在
    #     （服务端重启 / NAT 超时 / 负载均衡摘节点都会这样）
    #     这种情况 http.client 不会自己重连，必须靠 _fetch 的重试兜住。
    c8b = CountingHttpClient(LOCAL_CFG)
    c8b.get(base + "/get")
    pool8b = c8b._pool_for("http", "127.0.0.1", port)
    n_before = c8b.n_conn
    killed = 0
    for conn in list(pool8b._free):
        if conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)   # 只掐 TCP，不置 sock=None
                killed += 1
            except OSError:
                pass
    try:
        out = c8b.get(base + "/get")
        check("对端半死连接能被重试自愈",
              out == "ok" and killed == 1 and c8b.n_conn > n_before,
              "掐死 %d 条，建连接数 %d → %d" % (killed, n_before, c8b.n_conn))
    except Exception as exc:                                 # noqa: BLE001
        check("对端半死连接能被重试自愈", False, repr(exc))

    # 9. 双协议降级（巨潮 https 偶尔不通）
    c9 = CountingHttpClient(LOCAL_CFG)
    out = c9.get("http://127.0.0.1:%d/get" % port, dual_scheme=False)
    check("不带 dual_scheme 的普通请求正常", out == "ok")

    # 10. close() 清空池
    c10 = CountingHttpClient(LOCAL_CFG)
    c10.get(base + "/get")
    c10.close()
    pool = c10._pool_for("http", "127.0.0.1", port)
    check("close() 清空空闲连接", not pool._free and pool._n == 0)

    srv.shutdown()


# ---------------------------------------------------------------- 真网
TENCENT = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
           "?param=%s,day,,,120,qfq")
TENCENT_QUOTE = "https://qt.gtimg.cn/q="

# ★ 裸 6 位代码 —— 全仓库的口径（codes.py 的 docstring 写死了这条：
#   secid/tx_symbol 都期望裸代码，传 'sh600000' 进去会拼出 'szsh600000'）。
#   测试必须和 pipeline 用同一个口径、同一个 tx_symbol，
#   否则测的是"我拼的 URL 对不对"而不是"代码有没有被改坏"。
CODES = ["600000", "600036", "600519", "601318", "601398", "601857",
         "601988", "603259", "000001", "000002", "000333", "000651"]
HDR = {"Referer": "https://gu.qq.com/"}


def test_live(cfg: dict):
    """直接调**真实的数据源类**，验证换掉底层之后三个源都还能用。

    故意不用自己拼的 URL —— 那样测的是"我拼的 URL 对不对"，
    而不是"HttpClient 换掉 urllib 之后，巨潮的 POST / 双协议降级、
    腾讯的 GBK、K 线的 JSON 解析有没有被弄坏"。
    """
    print("\n[2] 真网连通性（走真实数据源类）", flush=True)
    from app.sources.codes import tx_symbol
    from app.sources.cninfo import CninfoSource
    from app.sources.quotes import MarketData

    c = HttpClient(cfg)

    # a0) 代码口径契约 —— 这次就是踩了它：把 'sh600000' 喂给 tx_symbol
    #     会拼出 'szsh600000'，接口回一句 v_pv_none_match，静默变成空数据。
    #     全仓库都用裸代码，这条断言把口径钉住。
    check("tx_symbol 口径：裸代码 → sh/sz 前缀",
          tx_symbol("600000") == "sh600000" and tx_symbol("000001") == "sz000001"
          and tx_symbol("830799") == "bj830799",
          "600000→%s 000001→%s 830799→%s"
          % (tx_symbol("600000"), tx_symbol("000001"), tx_symbol("830799")))

    # a) 腾讯 K 线（最热的那条路）
    try:
        sym = tx_symbol("600000")
        data = c.get_json(TENCENT % sym, HDR)
        rows = ((data.get("data") or {}).get(sym) or {}).get("qfqday") or []
        check("腾讯 K 线可取（600000）", len(rows) > 0, "%d 根" % len(rows))
    except Exception as exc:                                 # noqa: BLE001
        check("腾讯 K 线可取（600000）", False, repr(exc)[:70])

    # b) 腾讯行情批量（GBK，走 MarketData.market_caps）
    try:
        m = MarketData(http=c)
        caps = m.market_caps(["600000", "000001", "300750"])
        check("腾讯行情批量 + GBK 解码正常",
              len(caps) == 3 and all(v > 0 for v in caps.values()), str(caps))
    except Exception as exc:                                 # noqa: BLE001
        check("腾讯行情批量 + GBK 解码正常", False, repr(exc)[:70])

    # c) 巨潮 POST + dual_scheme（公告的备选源）
    try:
        src = CninfoSource(MarketData(http=c))
        res = src._fetch_page("sse", "2026-09-12~2026-09-12", 1)
        n = int(res.get("totalAnnouncement") or 0)
        check("巨潮 POST 查询可取", n > 0, "09-12 沪市 %d 条" % n)
    except Exception as exc:                                 # noqa: BLE001
        check("巨潮 POST 查询可取", False, repr(exc)[:70])

    # d) 东财：从沙箱**预期连不通**，只要求它快速失败、别挂死
    t0 = time.time()
    try:
        c.get_json("https://np-anotice-stock.eastmoney.com/api/security/ann"
                   "?page_size=1&page_index=1&ann_type=A", dual_scheme=True)
        em = "通（%.1fs）" % (time.time() - t0)
    except Exception as exc:                                 # noqa: BLE001
        em = "不通（%.1fs，%s）" % (time.time() - t0, type(exc).__name__)
    print("  ---- 东财：%s（沙箱不通属已知，不影响判定）" % em, flush=True)

    c.close()


def bench(cfg: dict, reps: int = 3):
    from app.sources.codes import tx_symbol

    n = len(CODES)
    urls = [TENCENT % tx_symbol(c) for c in CODES]

    def pooled_run():
        c = HttpClient(cfg)
        t0 = time.time()
        idx = [0]
        lk = threading.Lock()

        def work():
            while True:
                with lk:
                    i = idx[0]
                    idx[0] += 1
                if i >= n:
                    return
                c.get_json(urls[i], HDR)

        ts = [threading.Thread(target=work)
              for _ in range(min(cfg.get("conn_pool_size", 8), n))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        dt = time.time() - t0
        c.close()
        return dt

    def fresh_run():
        t0 = time.time()
        for url in urls:
            req = urllib.request.Request(url, headers=HDR)
            urllib.request.urlopen(req, timeout=30).read()
        return time.time() - t0

    fresh: list = []
    pool: list = []
    print("\n[3] 真网性能对照（%d 只股票 × %d 轮，交错，取最小值）" % (n, reps), flush=True)
    for r in range(reps):
        fresh.append(fresh_run())
        pool.append(pooled_run())
        print("  轮%d  新建连接·串行 %6.2fs   |   连接池×%d %5.2fs"
              % (r, fresh[-1], cfg.get("conn_pool_size", 8), pool[-1]), flush=True)

    fm, pm = min(fresh), min(pool)
    print("\n  基线（新建连接·串行）：%.3f s/只  → 3500 只约 %.1f 分钟" % (fm / n, fm / n * 3500 / 60))
    print("  连接池×%-2d            ：%.3f s/只  → 3500 只约 %.1f 分钟"
          % (cfg.get("conn_pool_size", 8), pm / n, pm / n * 3500 / 60))
    print("  加速比：%.1f 倍" % (fm / pm), flush=True)
    check("真网加速比 >= 3 倍", fm / pm >= 3.0, "%.1f 倍" % (fm / pm))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="加跑真网连通性 + 性能对照")
    ap.add_argument("--proxy", default=None, help="显式代理，如 http://host:port")
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()

    test_correctness()

    if args.live:
        cfg = dict(LOCAL_CFG)
        cfg["proxy_env_fallback"] = True
        if args.proxy:
            cfg["proxy"] = args.proxy
        test_live(cfg)
        bench(cfg, args.reps)

    bad = [r for r in RESULTS if not r[1]]
    print("\n===== 汇总：%d 项，通过 %d，失败 %d ====="
          % (len(RESULTS), len(RESULTS) - len(bad), len(bad)), flush=True)
    for name, _ok, detail in bad:
        print("  FAIL %s  %s" % (name, detail), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
