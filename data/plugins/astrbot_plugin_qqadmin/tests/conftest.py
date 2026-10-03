"""插件测试配置：把插件目录加入模块搜索路径"""

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PLUGIN_DIR.parent

for path in (PLUGINS_DIR, PLUGIN_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def clear_name_cache():
    """openid -> 昵称 缓存是进程内全局状态，用例间需要隔离"""
    from astrbot_plugin_qqadmin import utils

    utils._NAME_CACHE.clear()
    yield
    utils._NAME_CACHE.clear()
