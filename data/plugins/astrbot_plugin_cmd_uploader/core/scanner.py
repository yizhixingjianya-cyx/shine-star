"""扫描 AstrBot 已加载的全部指令。

AstrBot 把每个 ``@filter.command`` 处理器登记在 ``star_handlers_registry`` 里，
并在 ``StarHandlerMetadata.event_filters`` 中保留 ``CommandFilter``（含主指令名与别名）。
本模块据此汇总出「全机器人指令清单」，供面板展示与勾选上传。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot")

__all__ = ["collect_commands", "merge_commands"]


def _shortest(name: str, aliases: list[str]) -> str:
    """从主指令名与别名里挑最短的作为面板按钮名（沿用 CYX BOT「有别名只取最短」）。"""
    candidates = [c for c in [name, *aliases] if c]
    return min(candidates, key=lambda w: (len(w), w)) if candidates else name


def _plugin_display(metadata) -> str:
    """取插件展示名：优先 display_name，其次 name。"""
    if metadata is None:
        return ""
    return str(
        getattr(metadata, "display_name", None) or getattr(metadata, "name", "") or ""
    )


def collect_commands() -> list[dict[str, Any]]:
    """扫描全部已注册指令。

    返回元素结构::

        {name, panel_name, aliases, plugin, desc, scope, block}

    - ``name``：主指令名（``@filter.command(name)`` 的第一个参数）。
    - ``panel_name``：面板显示名（默认取最短触发词，可在面板里改）。
    - ``plugin``：注册该指令的插件名。
    - ``desc``：处理器 docstring 首行（也可在面板里改）。

    Returns:
        指令列表，按名称排序。
    """
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    found: dict[str, dict[str, Any]] = {}
    for handler in star_handlers_registry:
        if handler.event_type != EventType.AdapterMessageEvent:
            continue
        # 找出该 handler 上的 CommandFilter
        cmd_filter = None
        for event_filter in handler.event_filters:
            if isinstance(event_filter, CommandFilter):
                cmd_filter = event_filter
                break
        if cmd_filter is None:
            continue

        name = str(getattr(cmd_filter, "command_name", "") or "").strip()
        if not name:
            continue
        aliases = sorted(
            (str(a) for a in (getattr(cmd_filter, "alias", None) or set()) if str(a)),
            key=lambda w: (len(w), w),
        )
        if name in found:
            # 同名指令（不同插件/重复注册）合并别名，保留先到的插件信息
            existing = found[name]
            for alias in aliases:
                if alias not in existing["aliases"]:
                    existing["aliases"].append(alias)
            continue

        metadata = star_map.get(handler.handler_module_path)
        plugin_name = _plugin_display(metadata)
        desc = str(handler.desc or "").strip().splitlines()
        found[name] = {
            "name": name,
            "panel_name": _shortest(name, aliases),
            "aliases": aliases,
            "plugin": plugin_name,
            "desc": desc[0] if desc else "",
            "scope": ["group", "c2c"],
            "block": False,
        }

    return sorted(found.values(), key=lambda c: c["name"])


def merge_commands(saved: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """扫描结果 ∪ 用户已保存的编辑，得到最终指令清单。

    规则（沿用 CYX BOT 的合并逻辑）：

    - 扫到的指令：保留用户改过的 ``panel_name`` / ``desc`` / ``scope`` / ``block``，
      其余字段以扫描结果为准；
    - 仅存在于保存清单、本次没扫到的指令（例如停用插件）：保留为手工条目，不删除。

    Args:
        saved: 已保存的指令清单。

    Returns:
        合并后的指令列表（按名称排序）。
    """
    saved = [c for c in (saved or []) if isinstance(c, dict) and c.get("name")]
    prev = {str(c["name"]): c for c in saved}

    merged: list[dict[str, Any]] = []
    for auto in collect_commands():
        name = auto["name"]
        old = prev.pop(name, None) or {}
        auto["panel_name"] = str(old.get("panel_name") or auto["panel_name"]).strip()
        auto["desc"] = str(old.get("desc") or auto["desc"]).strip()
        auto["scope"] = list(old.get("scope") or auto["scope"])
        auto["block"] = bool(old.get("block"))
        merged.append(auto)

    # 手工 / 已卸载插件的条目原样保留
    for old in prev.values():
        keep = dict(old)
        keep.setdefault("aliases", [])
        keep.setdefault("panel_name", keep.get("name", ""))
        keep.setdefault("scope", ["group", "c2c"])
        keep.setdefault("block", False)
        keep["manual"] = True
        merged.append(keep)

    return sorted(merged, key=lambda c: c["name"])
