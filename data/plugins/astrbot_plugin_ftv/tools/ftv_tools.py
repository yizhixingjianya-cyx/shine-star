"""FTV 平台 -> AstrBot LLM 工具（含图片渲染与嵌入）。

在 cyxbot 的 ftv 插件里，这些能力走指令 + 本机渲染图片；这里把它封装成
AstrBot 的 function-calling 工具，供 AI 在对话中自动调用。

工具返回给 LLM 的是**文本**；当工具会产图片时：

- 若配置了本地图床（``image_host_url``），图片落盘后返回 markdown 图片语法
  ``![说明 #宽 #高](http://.../ftv/tmp/xxx.png)``，AI 可把它直接写进回复，
  支持 markdown 的平台即可把图片嵌进消息里；
- 另有 ``ftv_send_image`` 工具，调用后**直接**把渲染好的图片作为一条消息发出。

查询类工具全部复用 ``core.api.FtvAPI``。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api.message_components import Image, Plain
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..core import FtvService, image_host, render
from ..core.api import FtvError
from ..core.format import (
    format_user_info,
)

logger = logging.getLogger("astrbot")

__all__ = ["build_tools"]


def _prop(
    ptype: str, description: str, enum: list[str] | None = None
) -> dict[str, Any]:
    """构造一个 JSON schema 属性。"""
    p: dict[str, Any] = {"type": ptype, "description": description}
    if enum:
        p["enum"] = enum
    return p


def _make_tool(
    name: str,
    description: str,
    props: dict[str, dict],
    required: list[str] | None,
    handler: Callable[[dict], Awaitable[str]],
    *,
    with_context: bool = False,
):
    """动态构造一个 FunctionTool 子类。

    Args:
        name: 工具名称。
        description: 工具描述（给 LLM 看）。
        props: JSON schema 属性。
        required: 必填参数名。
        handler: 异步处理函数，入参为 kwargs 字典（``with_context=True`` 时额外接收
            第一个参数 ``context``），返回文本结果。
        with_context: 是否把 ``ContextWrapper`` 传给 handler（用于直接发消息）。

    Returns:
        FunctionTool 实例。
    """
    schema = {"type": "object", "properties": props, "required": required or []}

    async def _call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:  # noqa: ANN001
        try:
            if with_context:
                return await handler(context, kwargs)
            return await handler(kwargs)
        except FtvError as e:
            return f"[FTV 错误] {e}"
        except PermissionError as e:
            return f"[FTV 权限不足] {e}"
        except FileNotFoundError:
            return "[FTV] 未找到该资源（可能用户名/ID 不存在或内容已下架）"
        except Exception as e:  # noqa: BLE001
            logger.exception("[ftv tools] 工具调用异常")
            return f"[FTV 错误] {type(e).__name__}: {e}"

    cls = type(
        "_Concrete",
        (FunctionTool,),
        {
            "__module__": __name__,
            "__annotations__": {"name": str, "description": str, "parameters": dict},
            "name": name,
            "description": description,
            "parameters": Field(default_factory=lambda: schema),
            "call": _call,
        },
    )
    return dataclass(cls)()


def build_tools(service: FtvService) -> list[FunctionTool]:
    """根据 service 构建全部 FTV LLM 工具。

    Args:
        service: 持有 API 客户端与图床配置的服务对象。

    Returns:
        工具列表。
    """
    api = service.api
    tools: list[FunctionTool] = []

    def _query(
        grant: str,
        method_name: str,
        params_fn: Callable[[dict], dict] | None = None,
    ) -> Callable[[dict], Awaitable[str]]:
        """构造调用 FtvAPI 方法并渲染文本的处理函数。"""

        async def _h(kwargs: dict) -> str:
            # 未提供 params_fn 时，直接把工具入参透传给 API 方法
            # （工具 schema 的参数名与 API 方法参数名一致）
            params = params_fn(kwargs) if params_fn else dict(kwargs)
            method = getattr(api, method_name)
            data = await (method(**params) if params else method())
            return _render(data)

        return _h

    # ---------- 用户档案 / 角色 ----------
    tools.append(
        _make_tool(
            "furtv_user_profile",
            "查询 FTV/FurryWill 平台指定用户的公开资料档案（按平台用户名，不是 QQ 号）。"
            "【使用范围】用户想了解某毛装者的简介、物种、地区、喜好等信息时调用；"
            "参数必须是**平台用户名**（如 xj123）。若用户只给了模糊中文名/群昵称，"
            "请先用 furtv_search 找到用户名再调用。",
            {"username": _prop("string", "FTV 平台用户名，例如 xj123")},
            ["username"],
            _query("furtv.users", "get_user_profile"),
        )
    )
    tools.append(
        _make_tool(
            "furtv_user_characters",
            "查询指定用户名下注册的毛装角色列表（角色名、物种、世界观设定等）。"
            "【使用范围】用户想看某人名下**已注册的毛装角色**、说『TA 的崽/设子/角色』时调用。",
            {"username": _prop("string", "FTV 平台用户名")},
            ["username"],
            _query("furtv.characters", "get_user_characters"),
        )
    )
    tools.append(
        _make_tool(
            "furtv_search",
            "按关键词在 FTV 平台搜索公开档案。type 可选 all/user/fursuit/character。"
            "【使用范围】用户给出**关键词**要找平台上的公开档案、或需要用名字/昵称**反查用户名**时调用；"
            "返回的是列表，拿到结果后再决定是否用 furtv_user_profile 深挖。"
            "要正式结果用它，要输入补全用 furtv_search_suggestions。",
            {
                "q": _prop("string", "搜索关键词"),
                "type": _prop(
                    "string", "搜索范围", enum=["all", "user", "fursuit", "character"]
                ),
            },
            ["q"],
            _query(
                "furtv.discovery",
                "search",
                lambda kw: {"q": kw["q"], "type": kw.get("type", "all")},
            ),
        )
    )
    tools.append(
        _make_tool(
            "furtv_search_species",
            "按物种搜索公开档案，例如 狼、狐狸、猫 等。"
            "【使用范围】用户想**按物种浏览**档案时调用。",
            {"species": _prop("string", "物种关键词")},
            ["species"],
            _query("furtv.discovery", "search_by_species"),
        )
    )
    tools.append(
        _make_tool(
            "furtv_popular",
            "获取 FTV/FurryWill 兽频道『热门』页的兽装/人物档案榜单（按热度的兽装资料，不是图文动态）。"
            "【使用范围】用户说『看看热门毛毛』『热门榜』时用它；"
            "若要『一条 today/图文动态』请改用 furtv_today_explore，二者不要混用。",
            {"limit": _prop("integer", "返回条数，默认 10")},
            [],
            _query(
                "furtv.discovery",
                "get_popular",
                lambda kw: {"limit": int(kw.get("limit") or 10)},
            ),
        )
    )
    tools.append(
        _make_tool(
            "furtv_random",
            "随机获取若干兽装/人物档案，适合随机毛装者推荐。"
            "【使用范围】用户要**随机推荐**毛装者（如『随便来个毛毛』）时调用；count 控制数量。",
            {"count": _prop("integer", "随机数量，默认 1")},
            [],
            _query(
                "furtv.fursuit",
                "get_random",
                lambda kw: {"count": int(kw.get("count") or 1)},
            ),
        )
    )
    tools.append(
        _make_tool(
            "furtv_popular_locations",
            "获取 FTV 平台热门地区统计。"
            "【使用范围】用户想看**热门地区**分布统计时调用；无参数。",
            {},
            [],
            _query("furtv.discovery", "get_popular_locations"),
        )
    )
    tools.append(
        _make_tool(
            "furtv_user_like_status",
            "查询当前账号对某用户的点赞状态与点赞数。"
            "【使用范围】用户想知道**当前登录账号**对某用户的**点赞状态/点赞数**时调用；"
            "这是查账号自身状态，不是查资料。",
            {"username": _prop("string", "FTV 平台目标用户名")},
            ["username"],
            _query("furtv.fursuit", "get_user_like_status"),
        )
    )
    tools.append(
        _make_tool(
            "furtv_search_suggestions",
            "输入关键词获取 FTV 搜索自动补全建议（至少 2 个字符）。"
            "【使用范围】用户输入**未完成的关键词**、想要**补全建议**时调用；"
            "要正式搜索结果请用 furtv_search。",
            {"q": _prop("string", "搜索关键词，至少 2 个字符")},
            ["q"],
            _query("furtv.discovery", "get_search_suggestions"),
        )
    )

    # ---------- 学校 ----------
    tools.append(
        _make_tool(
            "furtv_search_schools",
            "按名称搜索 FTV 平台登记的学校信息。"
            "【使用范围】用户按**学校名称**查找 FTV 登记学校时调用。",
            {"query": _prop("string", "学校名称关键词")},
            ["query"],
            _query("furtv.schools", "search_schools"),
        )
    )

    # ---------- 聚会 ----------
    tools.append(
        _make_tool(
            "furtv_gatherings_yearly_stats",
            "获取当前年份 FTV 平台毛装聚会总数统计。"
            "【使用范围】用户问**今年聚会总数**统计时调用；无参数。",
            {},
            [],
            _query("furtv.gatherings", "get_gatherings_yearly_stats"),
        )
    )
    tools.append(
        _make_tool(
            "furtv_gatherings_monthly",
            "查询某年某月的毛装聚会列表。"
            "【使用范围】用户想查**某年某月**的聚会列表时调用；必须提供 year 与 month，"
            "拿到列表中的 id 后可再用 furtv_gathering_detail 看详情。",
            {
                "year": _prop("integer", "年份，如 2026"),
                "month": _prop("integer", "月份 1-12"),
            },
            ["year", "month"],
            _query(
                "furtv.gatherings",
                "get_gatherings_monthly",
                lambda kw: {"year": int(kw["year"]), "month": int(kw["month"])},
            ),
        )
    )
    tools.append(
        _make_tool(
            "furtv_gathering_detail",
            "查询某场毛装聚会的详情。"
            "【使用范围】用户想看**某一场聚会**详情时调用；需 gathering_id"
            "（从 furtv_gatherings_monthly 的结果里取）。",
            {"gathering_id": _prop("string", "聚会 ID")},
            ["gathering_id"],
            _query("furtv.gatherings", "get_gathering_detail"),
        )
    )

    # ---------- Today 公开内容 ----------
    tools.append(
        _make_tool(
            "furtv_today_explore",
            "查询兽频道 Today 图文动态的『探索』流。兽频道 Today ≈ 朋友圈/片刻的图文发布。"
            "【使用范围】用户说『推/发一条 today』『刷一条动态』时优先用它；"
            "exclude_today_id 可避免重复推送同一条。",
            {
                "limit": _prop("integer", "返回条数，上游上限 24，默认 10"),
                "exclude_today_id": _prop("string", "可选，排除某条 Today ID"),
            },
            [],
            _query(
                "furtv.today",
                "get_today_explore",
                lambda kw: {
                    "limit": int(kw.get("limit") or 10),
                    **(
                        {"exclude_today_id": kw["exclude_today_id"]}
                        if kw.get("exclude_today_id")
                        else {}
                    ),
                },
            ),
        )
    )
    tools.append(
        _make_tool(
            "furtv_today_feed",
            "查询兽频道 Today 图文动态的时间线信息流（可按日期翻页）。"
            "【使用范围】用户想浏览 **Today 时间线信息流**或按日期翻页时调用；"
            "要看单条详情用 furtv_today_detail。",
            {
                "scope": _prop("string", "信息流范围，默认 timeline"),
                "date": _prop("string", "可选，分页日期 YYYY-MM-DD"),
                "limit": _prop("integer", "返回条数，上游上限 20，默认 10"),
            },
            [],
            _query(
                "furtv.today",
                "get_today_feed",
                lambda kw: {
                    "limit": int(kw.get("limit") or 10),
                    **({"scope": kw["scope"]} if kw.get("scope") else {}),
                    **({"date": kw["date"]} if kw.get("date") else {}),
                },
            ),
        )
    )
    tools.append(
        _make_tool(
            "furtv_today_detail",
            "查询某条 Today 的详情（含正文与截图保护标记）。"
            "【使用范围】用户想看**某条 Today** 的详情时调用；需 today_id"
            "（从 furtv_today_explore / furtv_today_feed 的结果里取）。",
            {"today_id": _prop("string", "Today ID 或 UUID")},
            ["today_id"],
            _query(
                "furtv.today",
                "get_today_detail",
                lambda kw: {"today_id": kw["today_id"]},
            ),
        )
    )
    tools.append(
        _make_tool(
            "furtv_today_user_timeline",
            "查询某用户发布的 Today 动态时间线（用户 ID 或用户名）。"
            "【使用范围】用户想看**某用户发布过的 Today** 列表时调用；"
            "传用户 ID 或用户名。",
            {
                "user_identifier": _prop("string", "用户 ID 或用户名"),
                "limit": _prop("integer", "返回条数，上游上限 24，默认 10"),
            },
            ["user_identifier"],
            _query(
                "furtv.today",
                "get_today_user_timeline",
                lambda kw: {
                    "user_identifier": kw["user_identifier"],
                    "limit": int(kw.get("limit") or 10),
                },
            ),
        )
    )

    # ---------- 图片工具 ----------
    tools.append(
        _make_tool(
            "furtv_render_profile_card",
            "渲染一张 FTV 用户档案卡片图片，并可把图片嵌入回复。"
            "【使用范围】用户**要图片版**的用户档案卡片（如『发张档案图』）时用它；"
            "只要文字资料请用 furtv_user_profile。"
            "返回 markdown 图片语法（形如 `![档案 #宽 #高](http://.../ftv/tmp/xxx.png)`），"
            "你应把这段 markdown **原样**放进最终回复中，图片就会显示在聊天里。"
            "设置 send_to_chat=true 时会同时把图片作为一条消息直接发出。",
            {
                "username": _prop("string", "FTV 平台用户名"),
                "send_to_chat": _prop(
                    "boolean", "是否同时直接把图片发到聊天，默认 false"
                ),
            },
            ["username"],
            partial(_render_profile_card_tool, service),
            with_context=True,
        )
    )
    tools.append(
        _make_tool(
            "furtv_render_search_collage",
            "按关键词搜索 FTV 档案并渲染成拼图卡片，可把图片嵌入回复。"
            "【使用范围】用户想要**关键词搜索结果的拼图**图片时用它；"
            "只要文字列表用 furtv_search。"
            "返回 markdown 图片语法，应原样放进最终回复。send_to_chat=true 时同时直接发图。",
            {
                "q": _prop("string", "搜索关键词（用户名/角色名/物种等）"),
                "limit": _prop("integer", "拼图张数，默认 6，最多 9"),
                "send_to_chat": _prop(
                    "boolean", "是否同时直接把图片发到聊天，默认 false"
                ),
            },
            ["q"],
            partial(_render_collage_tool, service),
            with_context=True,
        )
    )
    tools.append(
        _make_tool(
            "ftv_send_image",
            "把 FTV 数据渲染成图片并**直接发送**到当前聊天（无需你把 markdown 写进回复）。"
            "【使用范围】确定要**直接把图片发到聊天**（而不是让你把 markdown 写进回复）时用它；"
            "若你想把图片嵌进自己的最终回复，请改用 furtv_render_profile_card / "
            "furtv_render_search_collage。"
            "支持：user_profile（指定用户名的档案大图）、search_collage（关键词搜到的档案拼图）、"
            "species_stats（物种统计图）。",
            {
                "kind": _prop(
                    "string",
                    "要渲染的图片类型",
                    enum=["user_profile", "search_collage", "species_stats"],
                ),
                "target": _prop(
                    "string",
                    "目标：用户名（user_profile）或关键词（search_collage）；物种统计可为空",
                ),
            },
            ["kind"],
            partial(_send_image_tool, service),
            with_context=True,
        )
    )

    logger.info(
        "[ftv tools] 已注册 %d 个工具：%s", len(tools), ", ".join(t.name for t in tools)
    )
    return tools


# ==================================================================== 渲染工具
async def _markdown_block(image, alt: str = "图片") -> str:
    """把 PIL 图片发布到图床并返回 markdown 图片语法；无图床返回空串。"""
    url = image_host.host_image(image)
    if not url:
        return ""
    w, h = image_host.md_display_size(image.width, image.height)
    return image_host.image_markdown(url, alt, w, h)


async def _send_pil_to_chat(
    context: ContextWrapper[AstrAgentContext], image, text: str = ""
) -> None:
    """把 PIL 图片作为一条消息直接发到当前会话。"""
    event = context.context.event
    comps = []
    if text:
        comps.append(Plain(text))
    url = image_host.host_image(image)
    if url:
        comps.append(Image.fromURL(url))
    else:
        from io import BytesIO

        buf = BytesIO()
        image.save(buf, format="PNG")
        comps.append(Image.fromBytes(buf.getvalue()))
    await event.send(event.chain_result(comps))


def _no_image_host_hint() -> str:
    return (
        "（未配置 image_host_url，无法把图片嵌入回复；"
        "可告诉用户稍后在聊天中查看，或改用 ftv_send_image 工具发图）"
    )


async def _build_profile_image(service: FtvService, username: str):
    """按用户名取资料并渲染档案大图。"""
    data = await service.api.get_user_profile(username)
    user = data.get("user") or data.get("data") or {}
    if not user:
        return None, {}
    img = await render.generate_profile_image(
        vertical_img_url=user.get("showcase_portrait", ""),
        avatar_url=user.get("avatar_url", ""),
        horizontal_img_url=user.get("showcase_landscape", ""),
        showcase_other_url=user.get("showcase_other", ""),
        profile_data={
            "id": user.get("id", username),
            "nickname": user.get("nickname", "未知昵称"),
            "username": user.get("username", username),
            "fursuit_species": user.get("fursuit_species", "未知"),
            "fursuit_birthday": user.get("fursuit_birthday", "无"),
            "fursuit_maker": user.get("fursuit_maker", "未知"),
            "location": user.get("location", "未知"),
            "introduction": user.get("introduction", "无"),
        },
        title_text="兽频道档案",
    )
    return img, user


async def _render_profile_card_tool(service: FtvService, context, kw: dict) -> str:
    """渲染用户档案卡片并返回 markdown / 直接发送。"""
    username = kw.get("username")
    img, user = await _build_profile_image(service, username)
    if img is None:
        return "[FTV] 未找到该用户或档案图渲染失败"

    info = format_user_info(user).rstrip("\n")
    if kw.get("send_to_chat"):
        await _send_pil_to_chat(context, img, f"用户档案：{username}")
        return f"[FTV] 已把「{username}」的档案图直接发送到聊天。"

    md = await _markdown_block(img, f"档案-{username}")
    lines = [f"用户档案：{username}", "", info]
    if md:
        lines += ["", "可直接把下面这行原样放进回复来嵌入图片：", md]
    else:
        lines.append(_no_image_host_hint())
    return "\n".join(lines)


async def _render_collage_tool(service: FtvService, context, kw: dict) -> str:
    """搜索并渲染拼图卡片。"""
    q = kw.get("q")
    limit = max(1, min(int(kw.get("limit") or 6), 9))
    data = await service.api.search(q)
    items = (data.get("users") or data.get("data") or [])[:limit]
    if not items:
        return f"[FTV] 没有搜索到与「{q}」相关的档案"
    img = await render.generate_users_collage(items)
    if img is None:
        return "[FTV] 拼图渲染失败"

    if kw.get("send_to_chat"):
        await _send_pil_to_chat(context, img, f"关键词「{q}」的档案拼图")
        return f"[FTV] 已把关键词「{q}」的档案拼图直接发送到聊天。"

    md = await _markdown_block(img, f"搜索-{q}")
    lines = [f"关键词「{q}」的档案拼图共 {len(items)} 张"]
    if md:
        lines += ["", "可直接把下面这行原样放进回复来嵌入图片：", md]
    else:
        lines.append(_no_image_host_hint())
    return "\n".join(lines)


async def _send_image_tool(service: FtvService, context, kw: dict) -> str:
    """渲染图片并直接发送到当前聊天。"""
    kind = kw.get("kind")
    target = (kw.get("target") or "").strip()

    if kind == "user_profile":
        if not target:
            return "[FTV] 需要提供用户名 target"
        img, _user = await _build_profile_image(service, target)
        alt = f"档案-{target}"
    elif kind == "search_collage":
        if not target:
            return "[FTV] 需要提供关键词 target"
        data = await service.api.search(target)
        items = (data.get("users") or data.get("data") or [])[:6]
        if not items:
            return f"[FTV] 没有搜索到与「{target}」相关的档案"
        img = await render.generate_users_collage(items)
        alt = f"搜索-{target}"
    elif kind == "species_stats":
        data = await service.api.get_species_list()
        species_list = data.get("species", []) or data.get("data", [])
        if not species_list:
            return "[FTV] 暂无物种数据"
        total = sum(s.get("count", 0) for s in species_list)
        images = await render.generate_species_stats_image(species_list, total)
        img = images[0] if images else None
        alt = "物种统计"
    else:
        return f"[FTV] 不支持的图片类型：{kind}"

    if img is None:
        return "[FTV] 图片渲染失败"

    await _send_pil_to_chat(context, img)
    return f"[FTV] 已生成图片（{alt}）并直接发送到聊天。"


# ==================================================================== 文本渲染
_MAX_STR = 200
_MAX_ITEMS = 10
_SKIP_KEYS = {"requestId", "success", "ok", "code", "message", "traceId", "error"}


def _cap(v, n: int = _MAX_STR) -> str:
    """截断过长字符串。"""
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt(value, level: int = 0, key: str | None = None) -> list[str]:
    """把任意 JSON 折成缩进行的列表（标量 -> ``key: value``，复合 -> 逐层展开）。"""
    pad = "  " * level
    lines: list[str] = []
    if value is None:
        return lines
    if isinstance(value, bool):
        lines.append(
            f"{pad}{key}: {'是' if value else '否'}"
            if key
            else f"{pad}{'是' if value else '否'}"
        )
        return lines
    if isinstance(value, (str, int, float)):
        lines.append(f"{pad}{key}: {_cap(value)}" if key else f"{pad}{_cap(value)}")
        return lines
    if isinstance(value, list):
        if not value:
            lines.append(f"{pad}{key}: （空）" if key else f"{pad}（空）")
            return lines
        if key:
            lines.append(f"{pad}{key}:")
        total = len(value)
        for i, item in enumerate(value[:_MAX_ITEMS]):
            sub = _fmt(item, level + 1)
            if not sub:
                continue
            if isinstance(item, dict):
                lines.append(f"{pad}  [{i + 1}]")
                lines.extend(sub)
            else:
                lines.append(f"{pad}  {i + 1}. {sub[0].lstrip()}")
                lines.extend(sub[1:])
        if total > _MAX_ITEMS:
            lines.append(f"{pad}  … 共 {total} 项，仅显示前 {_MAX_ITEMS} 项")
        return lines
    if isinstance(value, dict):
        items = [
            (k, v) for k, v in value.items() if k not in _SKIP_KEYS and v is not None
        ]
        if not items:
            return lines
        if key:
            lines.append(f"{pad}{key}:")
        for k, v in items:
            lines.extend(_fmt(v, level + 1, k))
        return lines
    lines.append(f"{pad}{key}: {_cap(value)}" if key else f"{pad}{_cap(value)}")
    return lines


def _render(data) -> str:
    """把接口返回 JSON 整理成给 LLM 的紧凑文本。"""
    text = "\n".join(_fmt(data)).strip()
    return text if text else "（接口返回空内容）"
