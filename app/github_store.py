"""GitHub-backed durable data store for disposable PandaStack sandboxes.

The repository stores only structured board data; the sandbox-local SQLite file is
an ephemeral working database. No credentials are ever written into the repo.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from . import db
from .timeutil import now_cn

CODE_RE = re.compile(r"^[0-9A-Za-z]{1,16}$")

DATA_DIR = "data"
# ★ GitHub Pages 从分支发布时，目录只能是 / 或 /docs —— 传 /site 会被 API 拒绝
#   （422: `/site` is not a possible value. Must be one of the following: /, /docs）。
#   所以站点输出目录用 docs/，这样不用额外引入 Actions workflow。
SITE_DIR = "docs"


def _git_env(token: str = ""):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    if token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = "AUTHORIZATION: basic " + base64.b64encode(
            ("x-access-token:" + token).encode()
        ).decode()
    return env


def _git(cwd, *args, token="", timeout=300, check=True):
    p = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env=_git_env(token),
    )
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-1000:])
    return p


def clone_or_open(repo: str, target: str, token: str = "", branch: str = "main") -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("BOARD_DATA_REPO 必须为 owner/repository")
    root = Path(target).resolve()
    if root.exists() and (root / ".git").is_dir():
        _git(root, "fetch", "origin", branch, token=token)
        _git(root, "checkout", "-q", branch, token=token)
        _git(root, "reset", "--hard", "origin/" + branch, token=token)
        return root
    if root.exists() and any(root.iterdir()):
        raise ValueError("数据仓库目录已存在但不是干净的 Git 仓库")
    root.parent.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "clone", "--depth", "1", "--branch", branch,
         f"https://github.com/{repo}.git", str(root), token=token)
    return root


def _json_dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _rows(sql, params=()):
    return [dict(r) for r in db.query(sql, params)]


def export_data(repo_dir: str):
    root = Path(repo_dir).resolve()
    data = root / DATA_DIR
    (data / "stocks").mkdir(parents=True, exist_ok=True)
    (data / "meta").mkdir(parents=True, exist_ok=True)

    stocks = {r["code"]: r for r in _rows("SELECT code,name,board,market_value,updated_at FROM stocks ORDER BY code")}
    anns = {}
    for r in _rows("SELECT ann_id,code,name,title,date,category,board,key_numbers,url,summary,created_at,source,is_noise FROM announcements ORDER BY code,date,ann_id"):
        anns.setdefault(r["code"], []).append(r)
    klines = {}
    for r in _rows("SELECT code,date,open,high,low,close,volume,change_pct,source,adjustment,snapshot_key FROM klines ORDER BY code,date"):
        klines.setdefault(r["code"], []).append(r)
    sync = {r["code"]: r for r in _rows("SELECT code,source,adjustment,snapshot_key,start_date,end_date,target_end,checked_at,status,error FROM kline_sync ORDER BY code")}
    caps = {}
    for r in _rows("SELECT code,date,market_value,updated_at FROM market_caps ORDER BY code,date"):
        caps.setdefault(r["code"], []).append(r)

    codes = sorted(set(stocks) | set(anns) | set(klines) | set(caps))
    # Remove stale per-stock files only inside the controlled data/stocks directory.
    wanted = {f"{c}.json" for c in codes if CODE_RE.fullmatch(c)}
    for p in (data / "stocks").glob("*.json"):
        if p.name not in wanted:
            p.unlink()

    for code in codes:
        if not CODE_RE.fullmatch(code):
            continue
        _json_dump(data / "stocks" / f"{code}.json", {
            "version": 1,
            "stock": stocks.get(code),
            "announcements": anns.get(code, []),
            "klines": klines.get(code, []),
            "market_caps": caps.get(code, []),
            "kline_sync": sync.get(code),
        })

    _json_dump(data / "meta" / "fetch_days.json", _rows(
        "SELECT date,source,fetched,expected,kept,complete,updated_at,error FROM fetch_days_v2 ORDER BY date,source"))
    _json_dump(data / "meta" / "day_fetch.json", _rows(
        "SELECT date,source,fetched,kept,updated_at FROM day_fetch ORDER BY date"))
    _json_dump(data / "meta" / "board_meta.json", _rows("SELECT key,value FROM board_meta ORDER BY key"))
    cap_count = db.scalar("SELECT COUNT(*) FROM market_caps", default=0)
    _json_dump(data / "meta" / "export.json", {
        "version": 1,
        "generated_at": now_cn().isoformat(),
        "stocks": len(codes),
        "announcements": db.scalar("SELECT COUNT(*) FROM announcements", default=0),
        "klines": db.scalar("SELECT COUNT(*) FROM klines", default=0),
        "market_caps": cap_count,
    })
    return {"ok": True, "stocks": len(codes),
            "announcements": db.scalar("SELECT COUNT(*) FROM announcements", default=0),
            "klines": db.scalar("SELECT COUNT(*) FROM klines", default=0),
            "market_caps": cap_count}


def _next_id(table):
    return int(db.scalar(f"SELECT COALESCE(MAX(id),0) FROM {table}", default=0) or 0) + 1


def import_data(repo_dir: str):
    root = Path(repo_dir).resolve() / DATA_DIR
    if not root.is_dir():
        return {"ok": True, "empty": True, "stocks": 0, "announcements": 0, "klines": 0}
    stock_dir = root / "stocks"
    if not stock_dir.is_dir():
        return {"ok": True, "empty": True, "stocks": 0, "announcements": 0, "klines": 0}

    db.init_db()
    ann_id = _next_id("announcements")
    kline_id = _next_id("klines")
    imported_stocks = imported_anns = imported_klines = imported_caps = 0
    # 已存在的主键集合：用来区分"真正插入"和"覆盖更新"。
    # 少了这三个集合就无法判断某一行是新增还是冲突，
    # 自增 id 会被冲突行白白吃掉，imported_* 也会退化成"读到的行数"。
    existing_anns = {r[0] for r in db.query("SELECT ann_id FROM announcements")}
    existing_klines = {(r[0], r[1]) for r in db.query("SELECT code,date FROM klines")}
    existing_caps = {(r[0], r[1]) for r in db.query("SELECT code,date FROM market_caps")}

    with db.transaction() as c:
        for path in sorted(stock_dir.glob("*.json")):
            if not CODE_RE.fullmatch(path.stem):
                continue
            obj = json.loads(path.read_text(encoding="utf-8"))
            stock = obj.get("stock")
            if stock:
                c.execute("""INSERT INTO stocks(code,name,board,market_value,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name,board=excluded.board,
                    market_value=CASE WHEN excluded.market_value>0 THEN excluded.market_value ELSE stocks.market_value END,
                    updated_at=excluded.updated_at""",
                    (stock.get("code"), stock.get("name", ""), stock.get("board"), stock.get("market_value"), stock.get("updated_at")))
                imported_stocks += 1
            for r in obj.get("announcements", []):
                # 只有真正新插入的行才消耗一个自增 id 并计入新增数。
                # 旧实现无条件 ann_id += 1，冲突更新的行也白吃 id，
                # 既让主键持续膨胀，也让 imported_anns 变成"读到的行数"而非新增数。
                is_new = r["ann_id"] not in existing_anns
                c.execute("""INSERT INTO announcements(id,ann_id,code,name,title,date,category,board,key_numbers,url,summary,created_at,source,is_noise)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ann_id) DO UPDATE SET
                    code=excluded.code,name=excluded.name,title=excluded.title,date=excluded.date,category=excluded.category,
                    board=excluded.board,key_numbers=excluded.key_numbers,url=excluded.url,summary=excluded.summary,
                    source=excluded.source,is_noise=excluded.is_noise""",
                    (ann_id, r["ann_id"], r["code"], r.get("name"), r["title"], r["date"], r["category"],
                     r.get("board"), r.get("key_numbers"), r.get("url"), r.get("summary"), r.get("created_at"),
                     r.get("source", "legacy"), int(r.get("is_noise", 0))))
                if is_new:
                    ann_id += 1
                    imported_anns += 1
                    existing_anns.add(r["ann_id"])
            for r in obj.get("klines", []):
                key = (r["code"], r["date"])
                is_new = key not in existing_klines
                c.execute("""INSERT INTO klines(id,code,date,open,high,low,close,volume,change_pct,source,adjustment,snapshot_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET
                    open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,
                    change_pct=excluded.change_pct,source=excluded.source,adjustment=excluded.adjustment,snapshot_key=excluded.snapshot_key""",
                    (kline_id, r["code"], r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"],
                     r.get("change_pct"), r.get("source", "legacy"), r.get("adjustment", "unknown"), r.get("snapshot_key", "legacy")))
                if is_new:
                    kline_id += 1
                    imported_klines += 1
                    existing_klines.add(key)
            for r in obj.get("market_caps", []):
                key = (r["code"], r["date"])
                if key in existing_caps:
                    continue
                c.execute("""INSERT INTO market_caps(code,date,market_value,updated_at)
                    VALUES(?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET
                    market_value=excluded.market_value,updated_at=excluded.updated_at""",
                    (r["code"], r["date"], r["market_value"], r.get("updated_at")))
                imported_caps += 1
                existing_caps.add(key)
            s = obj.get("kline_sync")
            if s:
                c.execute("""INSERT INTO kline_sync(code,source,adjustment,snapshot_key,start_date,end_date,target_end,checked_at,status,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
                    source=excluded.source,adjustment=excluded.adjustment,snapshot_key=excluded.snapshot_key,start_date=excluded.start_date,
                    end_date=excluded.end_date,target_end=excluded.target_end,checked_at=excluded.checked_at,status=excluded.status,error=excluded.error""",
                    tuple(s.get(k) for k in ("code","source","adjustment","snapshot_key","start_date","end_date","target_end","checked_at","status","error")))

        for r in json.loads((root / "meta" / "fetch_days.json").read_text(encoding="utf-8")) if (root / "meta" / "fetch_days.json").is_file() else []:
            c.execute("""INSERT INTO fetch_days_v2(date,source,fetched,expected,kept,complete,updated_at,error)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(date,source) DO UPDATE SET
                fetched=excluded.fetched,expected=excluded.expected,kept=excluded.kept,complete=excluded.complete,updated_at=excluded.updated_at,error=excluded.error""",
                (r["date"],r["source"],r["fetched"],r.get("expected"),r["kept"],r["complete"],r.get("updated_at"),r.get("error", "")))
        for r in json.loads((root / "meta" / "day_fetch.json").read_text(encoding="utf-8")) if (root / "meta" / "day_fetch.json").is_file() else []:
            c.execute("""INSERT INTO day_fetch(date,source,fetched,kept,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET source=excluded.source,fetched=excluded.fetched,kept=excluded.kept,updated_at=excluded.updated_at""",
                (r["date"],r.get("source"),r["fetched"],r["kept"],r.get("updated_at")))
        for r in json.loads((root / "meta" / "board_meta.json").read_text(encoding="utf-8")) if (root / "meta" / "board_meta.json").is_file() else []:
            c.execute("INSERT INTO board_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (r["key"], str(r["value"])))
    return {"ok": True, "stocks": imported_stocks, "announcements": imported_anns,
            "klines": imported_klines, "market_caps": imported_caps}


def commit_push(repo_dir: str, repo: str, token: str, message: str, branch: str = "main"):
    root = Path(repo_dir).resolve()
    _git(root, "config", "user.name", "panda-board-bot", token=token)
    _git(root, "config", "user.email", "bot@users.noreply.github.com", token=token)
    _git(root, "add", "-A", token=token)
    diff = _git(root, "diff", "--cached", "--quiet", token=token, check=False)
    if diff.returncode == 0:
        sha = _git(root, "rev-parse", "HEAD", token=token).stdout.strip()
        return {"ok": True, "changed": False, "commit": sha}
    if diff.returncode != 1:
        raise RuntimeError((diff.stderr or diff.stdout)[-1000:])
    _git(root, "commit", "-m", message, token=token)
    _git(root, "push", "origin", f"HEAD:refs/heads/{branch}", token=token)
    sha = _git(root, "rev-parse", "HEAD", token=token).stdout.strip()
    remote = _git(root, "ls-remote", "origin", f"refs/heads/{branch}", token=token).stdout.split()[0]
    if remote != sha:
        raise RuntimeError("GitHub 远端提交与本次提交不一致")
    return {"ok": True, "changed": True, "commit": sha}
