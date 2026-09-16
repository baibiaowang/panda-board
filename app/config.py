"""Validated configuration. Invalid rules must never silently disable collection."""
from __future__ import annotations
import copy
import os
import threading
import yaml
from .paths import rules_path

_lock = threading.RLock()
_cache = {}
_signature = None


def validate_config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError('rules.yaml 必须是键值对象')
    for name in ('fetch', 'network', 'display', 'build', 'classify', 'noise', 'numeric_extraction'):
        if name in cfg and not isinstance(cfg[name], dict):
            raise ValueError(f'rules.yaml: {name} 必须是对象')
    taxonomy = cfg.get('taxonomy')
    if not isinstance(taxonomy, list) or not taxonomy:
        raise ValueError('rules.yaml: taxonomy 不能为空')
    ids = [item.get('id') for item in taxonomy if isinstance(item, dict)]
    if len(ids) != len(taxonomy) or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError('taxonomy.id 必须非空且唯一')
    if cfg.get('classify', {}).get('multi_label', False):
        raise ValueError('当前存储使用单分类；multi_label=true 尚不支持')
    if cfg.get('classify', {}).get('default_category', 'other') not in ids:
        raise ValueError('default_category 必须存在于 taxonomy')
    if cfg.get('network', {}).get('timezone', 'Asia/Shanghai') != 'Asia/Shanghai':
        raise ValueError('A股业务时区必须为 Asia/Shanghai')
    return cfg


def rules_config():
    global _cache, _signature
    with _lock:
        path = rules_path()
        st = os.stat(path)
        signature = (path, st.st_mtime_ns, st.st_size)
        if signature != _signature:
            with open(path, encoding='utf-8') as f:
                cfg = validate_config(yaml.safe_load(f))
            _cache, _signature = cfg, signature
        return copy.deepcopy(_cache)


def get_fetch_config():
    return rules_config().get('fetch', {})


def get_network_config():
    return rules_config().get('network', {})


def get_display_config():
    return rules_config().get('display', {})


def get_build_config():
    return rules_config().get('build', {})


def site_title():
    return os.getenv('BOARD_SITE_TITLE', 'A股公告看板')
