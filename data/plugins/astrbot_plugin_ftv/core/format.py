"""响应格式化：把 FTV 接口返回整理成纯文本 / markdown。

移植自 cyxbot ftv 插件的 api 相关格式化逻辑（commands.py / today_commands.py），
保留原有的字段顺序与措辞。
"""

from __future__ import annotations

from datetime import datetime

__all__ = [
    "format_user_info",
    "format_user_info_md",
    "format_species_stats_text",
    "format_locations",
    "format_schools",
    "format_suggestions",
    "format_like_status",
    "format_health",
    "format_today_list",
    "format_today_single",
    "format_feed_blocks",
    "format_gathering_detail",
    "cap",
]

_MAX_LIST = 10
FEED_PAGE_SIZE = 6
_SKIP_KEYS = {
    "requestId",
    "success",
    "ok",
    "code",
    "message",
    "traceId",
    "error",
    "data",
}


def cap(s, n: int = 200) -> str:
    """截断过长的字符串。"""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ 用户资料
def format_user_info(item: dict) -> str:
    """把用户资料格式化为纯文本。"""
    name = item.get("nickname", item.get("username", "未知"))
    username = item.get("username", "")
    species = item.get("fursuit_species", "未知物种")
    location = item.get("location", "未知地区")

    text = f"{name}\n"
    if username and username != name:
        text += f"ID: {username}\n"
    text += f"物种：{species}\n"
    text += f"地区：{location}\n"

    maker = item.get("fursuit_maker")
    if maker:
        text += f"兽装制作：{maker}\n"
    view_count = item.get("view_count")
    if view_count is not None:
        text += f"浏览：{view_count}\n"
    introduction = item.get("introduction")
    if introduction:
        text += f"简介：{introduction}\n"
    contact_info = item.get("contact_info")
    if contact_info and contact_info.get("qq"):
        text += f"QQ: {contact_info.get('qq')}\n"
    created_at = item.get("created_at")
    if created_at:
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            text += f"加入：{dt.strftime('%Y-%m-%d')}\n"
        except Exception:  # noqa: BLE001
            pass
    return text


def format_user_info_md(item: dict) -> str:
    """用户资料的 markdown 版本。"""
    name = item.get("nickname", item.get("username", "未知"))
    username = item.get("username", "")
    species = item.get("fursuit_species", "未知物种")
    location = item.get("location", "未知地区")

    lines = [f"**{name}**"]
    if username and username != name:
        lines.append(f"`ID: {username}`")
    lines.append(f"**物种**：{species}")
    lines.append(f"**地区**：{location}")

    maker = item.get("fursuit_maker")
    if maker:
        lines.append(f"**兽装制作**：{maker}")
    view_count = item.get("view_count")
    if view_count is not None:
        lines.append(f"**浏览**：{view_count}")
    introduction = item.get("introduction")
    if introduction:
        lines.append(f"**简介**：{introduction}")
    contact_info = item.get("contact_info")
    if contact_info and contact_info.get("qq"):
        lines.append(f"**QQ**：{contact_info.get('qq')}")
    created_at = item.get("created_at")
    if created_at:
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            lines.append(f"**加入**：{dt.strftime('%Y-%m-%d')}")
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines)


# ------------------------------------------------------------------ 其它列表
def format_species_stats_text(species_list: list, total: int) -> str:
    """物种统计文本摘要。"""
    return (
        f"兽频道 物种统计总览\n物种总数：{len(species_list)} 种\n档案总数：{total} 个"
    )


def format_locations(rows: list, total_users: int) -> str:
    """热门地区文本。"""
    lines = [f"地区总数：{len(rows)} 个", f"总用户数：{total_users} 个", ""]
    lines.extend(f"{i}. {name} {count}" for i, (name, count) in enumerate(rows, 1))
    return "\n".join(lines).rstrip("\n")


def format_schools(schools: list, query: str) -> str:
    """学校搜索结果文本。"""
    lines = []
    for i, school in enumerate(schools[:10], 1):
        name = school.get("name") or "未知"
        short_name = school.get("short_name")
        lines.append(f"{i}. {name}" + (f"（{short_name}）" if short_name else ""))
        school_id = school.get("id")
        if school_id not in (None, ""):
            lines.append(f"   ID：{school_id}")
        location = school.get("location")
        if location:
            lines.append(f"   地区：{location}")
        school_type = school.get("type")
        if school_type:
            lines.append(f"   类型：{school_type}")
        student_count = school.get("student_count")
        lines.append(f"   用户：{'未知' if student_count is None else student_count}")
    body = "\n".join(lines)
    return f"学校搜索结果：'{query}'\n\n{body}"


def format_suggestions(data) -> str:
    """搜索建议文本。"""
    items = data if isinstance(data, list) else None
    if items is None and isinstance(data, dict):
        items = data.get("suggestions") or data.get("data")
        if isinstance(items, dict):
            items = items.get("suggestions")
    if not items:
        return "暂无相关搜索建议"
    lines = [f"共 {len(items)} 条建议："]
    for i, s in enumerate(items[:_MAX_LIST], 1):
        lines.append(f"{i}. {cap(s, 120)}")
    if len(items) > _MAX_LIST:
        lines.append(f"… 仅显示前 {_MAX_LIST} 条")
    return "\n".join(lines)


def _inner(data) -> dict:
    """兼容 ``{...}`` 与 ``{data: {...}}`` 两种返回外层。"""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def format_like_status(data) -> str:
    """点赞状态文本。"""
    root = _inner(data) or {}
    lines = []
    count = root.get("like_count", root.get("likes_count"))
    if count is not None:
        lines.append(f"当前点赞数：{count}")
    liked = root.get("is_liked")
    if liked is not None:
        lines.append("TA 已被本账号点赞" if liked else "本账号尚未给 TA 点赞")
    can = root.get("can_like")
    if can is not None:
        lines.append("现在可以点赞" if can else "当前不可点赞")
    days = root.get("days_until_can_like")
    if days is not None and not root.get("can_like"):
        lines.append(f"距可再次点赞还需 {days} 天")
    if not lines:
        for k, v in root.items():
            if k in _SKIP_KEYS or isinstance(v, (dict, list)):
                continue
            lines.append(f"{k}: {cap(v)}")
    return "\n".join(lines) or "（接口返回空内容）"


def format_health(data) -> str:
    """平台状态文本。"""
    root = _inner(data) or {}
    msg = root.get("message") or root.get("msg") or root.get("status") or "运行中"
    return f"FTV/FurryWill 平台状态：{cap(msg, 200)}"


# ------------------------------------------------------------------ Today
_CARD_LIST_KEYS = ("explore_items", "feed_items", "todays")


def _pick_card_list(data) -> list:
    """从响应里挑出卡片列表。"""
    if isinstance(data, list):
        return data
    inner = _inner(data)
    for key in _CARD_LIST_KEYS:
        value = inner.get(key)
        if isinstance(value, list) and value:
            return value
    if isinstance(inner.get("data"), list):
        return inner["data"]
    return []


def _card_merge(item: dict) -> dict:
    """把嵌套在 today / page 里的字段并入顶层。"""
    for key in ("today", "page"):
        nested = item.get(key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                item.setdefault(k, v)
    return item


def _card_author(item: dict) -> str:
    author = item.get("author") or item.get("user")
    if isinstance(author, dict):
        author = (
            author.get("nickname")
            or author.get("display_name")
            or author.get("username")
        )
    if not author:
        author = item.get("username") or item.get("nickname")
    return str(author) if author else ""


def _card_body(item: dict) -> str:
    body = item.get("caption")
    suffix = item.get("caption_suffix")
    if suffix and suffix not in (body or ""):
        body = f"{body} {suffix}" if body else suffix
    if not body:
        nested = item.get("page")
        if isinstance(nested, dict):
            body = nested.get("caption")
    return str(body) if body else ""


def _card_id(item: dict):
    return item.get("today_id") or item.get("today_uuid") or item.get("id")


def _today_card_text(item) -> str:
    if not isinstance(item, dict):
        return cap(item, 160)
    _card_merge(item)
    card_id = _card_id(item)
    author = _card_author(item)
    body = _card_body(item)
    controls = item.get("content_controls")
    allow = controls.get("allow_screenshot") if isinstance(controls, dict) else None

    parts = []
    if body:
        parts.append(f"「{cap(body, 150)}」")
    if author:
        parts.append(f"作者:{cap(author, 40)}")
    card_type = item.get("card_type") or item.get("type")
    if card_type:
        parts.append(f"类型:{cap(card_type, 24)}")
    if allow is not None:
        parts.append("可截图" if allow else "禁止截图")
    line = " ".join(parts) if parts else cap(item, 160)
    if card_id:
        line += (
            f"\n  · 详情ID: {cap(card_id, 48)}（发「动态详情 {cap(card_id, 48)}」查看）"
        )
    return line


def format_today_list(data) -> str:
    """Today 列表类响应 -> 文本。返回空串表示没有可展示的卡片。"""
    root = _inner(data)
    items = _pick_card_list(data)
    if not items:
        return ""
    lines = []
    meta_parts = []
    for key, label in (("user", "用户"), ("topic", "话题"), ("gathering", "聚会")):
        obj = root.get(key)
        name = ""
        if isinstance(obj, dict):
            name = str(
                obj.get("nickname")
                or obj.get("name")
                or obj.get("username")
                or obj.get("title")
                or ""
            )
        if name:
            meta_parts.append(f"{label}：{cap(name, 80)}")
    if meta_parts:
        lines.append("｜".join(meta_parts))
    if not meta_parts and isinstance(root.get("today"), dict):
        lines.append("【动态内容】")
    for i, item in enumerate(items[:_MAX_LIST], 1):
        lines.append(f"{i}. {_today_card_text(item)}")
    total = len(items)
    if total > _MAX_LIST:
        lines.append(f"\n… 共 {total} 条，仅显示前 {_MAX_LIST} 条")
    return "\n".join(lines).strip()


def format_today_single(data) -> str:
    """Today 详情/单条 -> 文本。"""
    root = _inner(data)
    today = root.get("today")
    if not isinstance(today, dict):
        today = root
    return _today_card_text(today) or ""


def format_feed_blocks(items: list, page: int = 1) -> str:
    """把动态流某一页渲染成 markdown：每条一个 text 代码块。"""
    start = (max(1, page) - 1) * FEED_PAGE_SIZE
    chunk = items[start : start + FEED_PAGE_SIZE]
    blocks = []
    for index, item in enumerate(chunk, 1):
        if isinstance(item, dict):
            _card_merge(item)
            author = _card_author(item) or "未知"
            body = _card_body(item) or "（无正文）"
        else:
            author, body = "未知", cap(item, 150)
        blocks.append(
            f"```text\n{index}：作者：{cap(author, 40)}\n内容：{cap(body, 150)}\n```"
        )
    return "\n\n".join(blocks)


def format_gathering_detail(gathering: dict) -> tuple[str, str]:
    """聚会详情 -> (纯文本, markdown)。"""
    title = gathering.get("title", "未知聚会")
    day = gathering.get("day", "未知")
    location = gathering.get("locationPublic", gathering.get("location", "未知地点"))
    description = gathering.get("description", "")
    result = f"{title}\n日期：{day}\n地点：{location}\n"
    md = f"**{title}**\n日期：{day}\n地点：{location}\n"
    if description:
        result += f"简介：{description}\n"
        md += f"简介：{description}\n"
    return result, md
