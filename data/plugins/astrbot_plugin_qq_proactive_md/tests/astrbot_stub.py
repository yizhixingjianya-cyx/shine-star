"""联调用的 AstrBot 运行时桩（stub）。

AstrBot 主程序依赖较重（数据库、Web 框架等），在纯 Python 环境下无法整体导入。
这里把 AstrBot 中**重依赖**的部分替换为轻量桩，并尽量复用仓库中的真实实现：

* 能找到 AstrBot 源码时（在仓库里开发，或通过环境变量 ``QQMD_ASTRBOT_ROOT`` 指定），
  ``astrbot.core.agent.tool`` / ``astrbot.core.agent.run_context`` 使用仓库里的**真实实现**，
  这样联调校验的就是真实基类行为（例如 pydantic dataclass 的字段声明）。
* 找不到源码时（例如压缩包被解到任意目录），退化为等价的本地轻量实现，让测试仍然可跑。

被替换的固定项：
  astrbot.api / astrbot.api.star        —— 插件注册与日志
  astrbot.core.astr_agent_context       —— 仅作为泛型参数使用
  astrbot.core.message.message_event_result —— 仅作为返回类型标注
"""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path
from typing import Any, Generic

try:  # Python 3.10 的标准库 TypeVar 还不支持 default=
    from typing_extensions import TypeVar

    TContext = TypeVar("TContext", default=Any)
except Exception:  # pragma: no cover - typing_extensions 不可用时的兜底
    from typing import TypeVar as _StdTypeVar

    TContext = _StdTypeVar("TContext")

PLUGIN_DIR = Path(__file__).resolve().parents[1]
# 用固定的合成包名加载插件，这样无论插件目录是 data/plugins/<name> 还是被解压到
# 任意目录，相对导入（from .qq_md_core import ...）都能正常工作。
PACKAGE_NAME = "qq_proactive_md_under_test"

_installed = False


def _locate_astrbot_root() -> Path | None:
    """定位 AstrBot 源码根目录。

    优先读取环境变量 ``QQMD_ASTRBOT_ROOT``，其次从插件目录逐级向上查找
    ``astrbot/core/agent/tool.py``。

    Returns:
        源码根目录；找不到时返回 ``None``。
    """
    from_env = os.environ.get("QQMD_ASTRBOT_ROOT")
    candidates: list[Path] = []
    if from_env:
        candidates.append(Path(from_env))
    candidates.extend(PLUGIN_DIR.parents)
    for candidate in candidates:
        if (candidate / "astrbot" / "core" / "agent" / "tool.py").is_file():
            return candidate
    return None


def _make_package_stub(name: str, search_path: list[str] | None = None) -> types.ModuleType:
    """创建一个包桩模块。

    Args:
        name: 模块全名。
        search_path: ``__path__`` 内容。

    Returns:
        新建的模块对象。
    """
    module = types.ModuleType(name)
    module.__path__ = search_path or []
    sys.modules[name] = module
    return module


def _install_local_agent_shims() -> None:
    """在没有 AstrBot 源码时，安装等价的 FunctionTool / ContextWrapper 实现。"""
    from pydantic import Field
    from pydantic.dataclasses import dataclass as pydantic_dataclass

    _make_package_stub("astrbot.core.agent")

    @pydantic_dataclass
    class FunctionTool(Generic[TContext]):
        """仓库 ``astrbot.core.agent.tool.FunctionTool`` 的等价精简版。"""

        name: str
        description: str
        parameters: dict
        handler: Any = None
        handler_module_path: str | None = None
        active: bool = True
        is_background_task: bool = False

        async def call(self, context: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

    tool_stub = types.ModuleType("astrbot.core.agent.tool")
    tool_stub.FunctionTool = FunctionTool
    tool_stub.ToolExecResult = str | None
    tool_stub.ToolSet = object
    sys.modules["astrbot.core.agent.tool"] = tool_stub

    @pydantic_dataclass
    class ContextWrapper(Generic[TContext]):
        """仓库 ``astrbot.core.agent.run_context.ContextWrapper`` 的等价精简版。"""

        context: TContext
        messages: list = Field(default_factory=list)
        tool_call_timeout: int = 120

    run_context_stub = types.ModuleType("astrbot.core.agent.run_context")
    run_context_stub.ContextWrapper = ContextWrapper
    sys.modules["astrbot.core.agent.run_context"] = run_context_stub


def _install_astrbot_stubs() -> None:
    """把 AstrBot 的重依赖模块替换为桩，保留可真实运行的部分。"""
    repo_root = _locate_astrbot_root()

    if repo_root is not None:
        for path in (PLUGIN_DIR, repo_root):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        import astrbot  # noqa: F401 - 真实包，__init__ 仅设置 logger

        # astrbot.core.__init__ 会拉起数据库与 Web 层，这里用一个只暴露 __path__ 的桩替代。
        core_stub = types.ModuleType("astrbot.core")
        core_stub.__path__ = [str(repo_root / "astrbot" / "core")]
        sys.modules["astrbot.core"] = core_stub
    else:
        logging.getLogger(__name__).warning(
            "未找到 AstrBot 源码，联调测试将使用本地精简版 FunctionTool 基类。",
        )
        astrbot_pkg = types.ModuleType("astrbot")
        astrbot_pkg.logger = logging.getLogger("astrbot")
        sys.modules["astrbot"] = astrbot_pkg
        astrbot_pkg.__path__ = []
        _make_package_stub("astrbot.core")
        if str(PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(PLUGIN_DIR))
        _install_local_agent_shims()

    astrbot_module = sys.modules["astrbot"]

    api_stub = types.ModuleType("astrbot.api")
    api_stub.logger = logging.getLogger("astrbot_plugin_qq_proactive_md")
    api_stub.AstrBotConfig = dict
    sys.modules["astrbot.api"] = api_stub
    astrbot_module.api = api_stub

    class _Star:
        def __init__(self, context=None):
            self.context = context

    class _Context:
        def __init__(self):
            self.tools = []

        def add_llm_tools(self, *tools):
            self.tools.extend(tools)

    def _register(name, author, desc, version):
        def _decorator(cls):
            cls.plugin_name = name
            cls.plugin_version = version
            return cls

        return _decorator

    star_stub = types.ModuleType("astrbot.api.star")
    star_stub.Context = _Context
    star_stub.Star = _Star
    star_stub.register = _register
    star_stub.StarTools = object
    sys.modules["astrbot.api.star"] = star_stub

    # 真实的 AstrAgentContext 会拉起 star.context（含平台管理器、Provider 管理器等），
    # 这里只作为 FunctionTool 的泛型参数使用，用轻量桩即可。
    class AstrAgentContext:  # noqa: D101 - 仅作类型参数
        pass

    agent_ctx_stub = types.ModuleType("astrbot.core.astr_agent_context")
    agent_ctx_stub.AstrAgentContext = AstrAgentContext
    sys.modules["astrbot.core.astr_agent_context"] = agent_ctx_stub

    # astrbot.core.agent.tool 需要 MessageEventResult（仅作返回类型标注）。
    # 真实实现会连带拉起 astrbot.core.config / file_token_service 等重依赖，
    # 这里同样用轻量桩替代。
    class MessageEventResult:  # noqa: D101 - 仅作类型标注
        pass

    _make_package_stub("astrbot.core.message")
    mer_stub = types.ModuleType("astrbot.core.message.message_event_result")
    mer_stub.MessageEventResult = MessageEventResult
    mer_stub.MessageChain = object
    sys.modules["astrbot.core.message.message_event_result"] = mer_stub


def bootstrap() -> None:
    """幂等地安装桩并让插件包可被导入。"""
    global _installed
    if _installed:
        return
    _install_astrbot_stubs()
    _installed = True


def load_main_module():
    """导入插件 ``main.py``，返回模块对象。

    以合成包的形式加载，等价于 AstrBot 通过 ``main.py`` 载入插件的方式。

    Returns:
        插件主模块。
    """
    bootstrap()
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "main.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 目录结构异常
        raise ImportError(f"无法加载插件主模块：{PLUGIN_DIR / 'main.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def load_core_module():
    """返回与 ``main.py`` 使用同一个实例的 ``qq_md_core`` 模块。

    Returns:
        插件核心模块。
    """
    load_main_module()
    return sys.modules[f"{PACKAGE_NAME}.qq_md_core"]
