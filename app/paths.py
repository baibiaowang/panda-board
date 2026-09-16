"""路径收敛点 —— 全项目唯一的路径来源。

本项目的运行模型是「一次性沙箱」，只有一个根目录：

  工作目录（临时）  SQLite、锁、缓存都在这里。沙箱销毁后随之消失，
                    因为真正的长期存储是 GitHub 数据仓库。
                    优先级：
                      1. BOARD_DATA_DIR（调度脚本显式指定，最可靠）
                      2. <repo>/work（本地开发兜底）

  产物目录（临时）  静态站点产物，由 cycle 直接指向数据仓库的 site/ 目录。

★ 这里没有「持久卷」。任何让数据库留在本地的设计，都会让下一轮从空库开始，
  与增量更新模型直接冲突。
"""
from __future__ import annotations

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def data_dir() -> str:
    """临时工作目录。沙箱内应指向 /workspace/panda-board-work 这类一次性路径。"""
    env = os.getenv("BOARD_DATA_DIR", "").strip()
    if env:
        return _ensure(env)
    return _ensure(os.path.join(BASE_DIR, "work"))


def db_path() -> str:
    return os.path.join(data_dir(), "board.db")


def lock_path() -> str:
    return os.path.join(data_dir(), ".update.lock")


def cache_path(name: str) -> str:
    """工作目录内的 JSON 缓存（市值、K线无数据名单）。"""
    return os.path.join(data_dir(), name)


def site_dir() -> str:
    """静态产物目录。cycle 会显式设置成数据仓库的 site/。"""
    return _ensure(os.getenv("BOARD_SITE_DIR", "").strip()
                   or os.path.join(BASE_DIR, "dist"))


def rules_path() -> str:
    return os.path.join(BASE_DIR, "config", "rules.yaml")


def web_dir() -> str:
    return os.path.join(BASE_DIR, "web")
