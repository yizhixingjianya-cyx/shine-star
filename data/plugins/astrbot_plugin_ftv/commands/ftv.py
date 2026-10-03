"""FTV 频道指令（由 cyxbot ftv 插件移植）。

保留原指令名、别名与回复文案，尽量复刻原发送格式：
- 支持 markdown 的平台（QQ 官方等）走「markdown 图片 + 正文」一条消息；
- 其它平台回落「文本段 + 图片组件」。

注意：这些 ``@filter.command`` 处理器必须直接定义在插件主类（``Star`` 子类）上，
因为 AstrBot 会在加载时把处理器绑定到插件实例（``partial(handler, star_cls)``）。
因此这里做成 **mixin**：``class FtvPlugin(FtvCommandMixin, Star)``，
方法内通过 ``self.service`` 访问共享的 API / 图床。
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain

from ..core import image_host, render
from ..core.format import (
    format_gathering_detail,
    format_like_status,
    format_locations,
    format_schools,
    format_species_stats_text,
    format_suggestions,
    format_today_list,
    format_today_single,
    format_user_info,
    format_user_info_md,
)

logger = logging.getLogger("astrbot")

__all__ = ["FtvCommandMixin"]


def _arg(event: AstrMessageEvent) -> str:
    """取指令后的原始参数文本（去掉指令名）。"""
    msg = event.get_message_str().strip()
    parts = msg.split(" ", 1)
    return parts[1].strip() if len(parts) > 1 else ""


class FtvCommandMixin:
    """ftv 指令处理器（mixin，期望宿主提供 ``self.service``）。"""

    service = None  # 由宿主 Star 类注入

    # ---------------------------------------------------------- 发送助手
    async def _send_text(self, event: AstrMessageEvent, text: str, md: str = ""):
        """发一条文本消息（有 md 时优先走 markdown）。"""
        if md:
            chain = event.make_result().message(md)
            chain.use_markdown(True)
            await event.send(chain)
        else:
            await event.send(event.plain_result(text))

    async def _send_image_text(
        self,
        event: AstrMessageEvent,
        image,
        text: str = "",
        md: str = "",
        alt: str = "档案图",
    ):
        """发「图 + 文」：优先图床 + markdown，其次本地图片组件。"""
        url = image_host.host_image(image) if image is not None else None
        if url:
            w, h = image_host.md_display_size(image.width, image.height)
            body = image_host.image_markdown(url, alt, w, h)
            chain = event.make_result().message(f"{body}\n\n{md or text}")
            chain.use_markdown(True)
            await event.send(chain)
            return
        # 回落：普通消息，文本在前、图片在后
        comps = []
        if text:
            comps.append(Plain(text))
        if image is not None:
            buf = BytesIO()
            image.save(buf, format="PNG")
            from astrbot.api.message_components import Image

            comps.append(Image.fromBytes(buf.getvalue()))
        if comps:
            await event.send(event.chain_result(comps))

    # ---------------------------------------------------------- 热门
    @filter.command("热门", alias={"热门推荐"})
    async def popular(self, event: AstrMessageEvent):
        """获取热门推荐档案。"""
        arg = _arg(event)
        limit = 10
        if arg:
            try:
                limit = max(1, min(int(arg), 50))
            except ValueError:
                pass
        try:
            data = await self.service.api.get_popular(limit)
            items = data.get("users") or data.get("fursuits") or data.get("data") or []
            if "fursuit" in data:
                items = [data.get("fursuit", {})]
            if not items:
                await self._send_text(event, "暂无热门推荐")
                return
            await self._send_cards(event, items, "推荐档案")
        except PermissionError as e:
            await self._send_text(event, f"端点权限故障，请联系 bot 管理员：{e}")
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"啊呀，似乎出问题了呢：{e}")

    # ---------------------------------------------------------- 随机
    @filter.command("随机", alias={"随机档案"})
    async def random(self, event: AstrMessageEvent):
        """获取随机推荐档案。"""
        arg = _arg(event)
        count = 1
        if arg:
            try:
                count = max(1, min(int(arg), 20))
            except ValueError:
                pass
        try:
            data = await self.service.api.get_random(count)
            items = (
                [data.get("fursuit", {})] if "fursuit" in data else data.get("data", [])
            )
            if not items:
                await self._send_text(event, "暂无随机推荐")
                return
            await self._send_cards(event, items, "随机档案")
        except PermissionError as e:
            await self._send_text(event, f"权限不足：{e}")
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    # ---------------------------------------------------------- 物种
    @filter.command("物种", alias={"物种搜索"})
    async def species(self, event: AstrMessageEvent):
        """按物种搜索档案。"""
        args = _arg(event).split()
        if not args:
            await self._send_text(event, "请提供物种名称，例如：/物种 狼")
            return
        species = args[0]
        page = 1
        if len(args) > 1:
            try:
                page = int(args[1])
            except ValueError:
                pass
        try:
            data = await self.service.api.search_by_species(species, page=page)
            items = data.get("users") or data.get("data") or []
            if not items:
                await self._send_text(event, "什么都没有找到呢UwU")
                return
            await self._send_cards(event, items[:10], f"物种'{species}'搜索")
        except PermissionError as e:
            await self._send_text(event, f"端点权限不足，请联系 bot 管理员{e}")
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"搜索失败：{e}")

    # ---------------------------------------------------------- 搜索
    @filter.command("搜索", alias={"兽搜索"})
    async def search(self, event: AstrMessageEvent):
        """按关键词搜索档案。"""
        args = _arg(event).split()
        if not args:
            await self._send_text(event, "请提供搜索关键词，例如：/搜索 毛毛")
            return
        q = args[0]
        search_type = args[1] if len(args) > 1 else "all"
        try:
            data = await self.service.api.search(q, type=search_type)
            items = data.get("users") or data.get("data") or []
            if not items:
                await self._send_text(event, "什么都没有找到呢UwU")
                return
            await self._send_cards(event, items[:10], f"关键词'{q}'搜索")
        except PermissionError as e:
            await self._send_text(event, f"权限不足：{e}")
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"搜索失败：{e}")

    # ---------------------------------------------------------- 热门地区
    @filter.command("热门地区")
    async def locations(self, event: AstrMessageEvent):
        """热门地区统计。"""
        try:
            raw = await self.service.api.get_popular_locations()
            inner = (
                raw.get("data")
                if isinstance(raw, dict) and isinstance(raw.get("data"), (dict, list))
                else raw
            )
            sources = [s for s in (inner, raw) if isinstance(s, (dict, list))]
            locations = []
            for source in sources:
                if isinstance(source, list):
                    locations = source
                else:
                    for key in (
                        "popular_cities",
                        "popular_provinces",
                        "locations",
                        "items",
                    ):
                        value = source.get(key)
                        if isinstance(value, list) and value:
                            locations = value
                            break
                if locations:
                    break
            if not locations:
                await self._send_text(event, "暂无热门地区数据")
                return
            rows = []
            for loc in locations:
                if not isinstance(loc, dict):
                    continue
                name = (
                    f"{loc.get('province') or ''}{loc.get('city') or ''}"
                    or loc.get("name")
                    or "未知"
                )
                rows.append((name, loc.get("count", 0)))
            total_users = 0
            for source in sources:
                if not isinstance(source, dict):
                    continue
                for key in ("total_users", "totalUsers", "total", "total_count"):
                    value = source.get(key)
                    if isinstance(value, (int, float)) and value > 0:
                        total_users = int(value)
                        break
                if total_users:
                    break
            if not total_users:
                total_users = sum(c for _, c in rows if isinstance(c, (int, float)))
            body = format_locations(rows, total_users)
            md = f"**兽频道 热门地区统计**\n\n```text\n{body}\n```"
            await self._send_text(event, body, md)
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    # ---------------------------------------------------------- 物种列表
    @filter.command("物种列表", alias={"物种统计"})
    async def species_list(self, event: AstrMessageEvent):
        """物种统计图。"""
        try:
            data = await self.service.api.get_species_list()
            species_list = data.get("species", []) or data.get("data", [])
            if not species_list:
                await self._send_text(event, "暂无物种数据")
                return
            total = sum(s.get("count", 0) for s in species_list)
            images = await render.generate_species_stats_image(species_list, total)
            text = format_species_stats_text(species_list, total)
            if images:
                await self._send_images(event, images, text)
                return
            await self._send_text(event, text)
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    # ---------------------------------------------------------- 学校
    @filter.command("学校", alias={"学校搜索"})
    async def school(self, event: AstrMessageEvent):
        """按学校搜索档案。"""
        query = _arg(event)
        if not query:
            await self._send_text(event, "请提供学校名称，例如：/学校 北京大学")
            return
        try:
            data = await self.service.api.search_schools(query)
            schools = data.get("schools") or data.get("data") or []
            if not schools:
                await self._send_text(event, f"未找到学校 '{query}'")
                return
            body = format_schools(schools, query)
            md = f"**学校搜索结果：「{query}」**\n\n```text\n{body}\n```"
            await self._send_text(event, body, md)
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"搜索失败：{e}")

    # ---------------------------------------------------------- 兽档案
    @filter.command("兽档案", alias={"用户信息"})
    async def user_profile(self, event: AstrMessageEvent):
        """查看用户兽档案（含档案图）。"""
        username = _arg(event)
        if not username:
            await self._send_text(event, "请提供用户名，例如：/兽档案 example")
            return
        try:
            await self._show_user_profile(event, username)
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    async def _show_user_profile(self, event: AstrMessageEvent, username: str) -> None:
        data = await self.service.api.get_user_profile(username)
        user = data.get("user") or data.get("data") or {}
        if not user:
            await self._send_text(event, "什么都没有找到呢UwU")
            return
        profile_data = {
            "id": user.get("id", username),
            "nickname": user.get("nickname", "未知昵称"),
            "username": user.get("username", username),
            "fursuit_species": user.get("fursuit_species", "未知"),
            "fursuit_birthday": user.get("fursuit_birthday", "无"),
            "fursuit_maker": user.get("fursuit_maker", "未知"),
            "location": user.get("location", "未知"),
            "introduction": user.get("introduction", "无"),
            "interests": user.get("interests", []),
        }
        img = await render.generate_profile_image(
            vertical_img_url=user.get("showcase_portrait", ""),
            avatar_url=user.get("avatar_url", ""),
            horizontal_img_url=user.get("showcase_landscape", ""),
            showcase_other_url=user.get("showcase_other", ""),
            profile_data=profile_data,
            title_text="兽频道档案",
        )
        info_text = format_user_info(user).rstrip("\n")
        text = f"用户档案：{username}\n\n{info_text}"
        md = f"**用户档案：{username}**\n\n```text\n{info_text}\n```"
        if img:
            await self._send_image_text(event, img, text, md, alt="档案图")
        else:
            await self._send_text(event, text, md)

    # ---------------------------------------------------------- 崽崽
    @filter.command("崽崽", alias={"角色列表"})
    async def user_characters(self, event: AstrMessageEvent):
        """查看用户角色列表。"""
        username = _arg(event)
        if not username:
            await self._send_text(event, "请提供用户名，例如：/崽崽 example")
            return
        try:
            data = await self.service.api.get_user_characters(username)
            characters = data.get("characters") or data.get("data") or []
            if not characters:
                await self._send_text(event, "什么都没有找到呢UwU")
                return
            await self._send_cards(event, characters, f"{username}的角色")
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    # ---------------------------------------------------------- 聚会
    @filter.command("聚会统计")
    async def gatherings_stats(self, event: AstrMessageEvent):
        """聚会年度统计。"""
        try:
            data = await self.service.api.get_gatherings_yearly_stats()
            stats = data.get("data") or (
                {"total": data.get("total", 0)} if "total" in data else {}
            )
            if not stats:
                await self._send_text(event, "获取聚会统计失败")
                return
            result = (
                "聚会年度统计\n\n"
                f"今年聚会总数：{stats.get('total', 0)}\n"
                f"参与人数：{stats.get('participants', 0)}\n"
            )
            md = (
                "**聚会年度统计**\n\n"
                f"今年聚会总数：**{stats.get('total', 0)}**\n"
                f"参与人数：**{stats.get('participants', 0)}**\n"
            )
            await self._send_text(event, result, md)
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("本月聚会")
    async def monthly_gatherings(self, event: AstrMessageEvent):
        """本月聚会列表。"""
        from datetime import datetime

        now = datetime.now()
        try:
            data = await self.service.api.get_gatherings_monthly(now.year, now.month)
            content = data.get("data")
            gatherings = data.get("gatherings")
            if gatherings is None:
                if isinstance(content, list):
                    gatherings = content
                elif isinstance(content, dict):
                    gatherings = content.get("gatherings", [])
                else:
                    gatherings = []
            if not gatherings:
                await self._send_text(event, f"{now.year}年{now.month}月暂无聚会")
                return
            result = f"{now.year}年{now.month}月聚会列表\n\n"
            md = f"**{now.year}年{now.month}月聚会列表**\n\n"
            for i, g in enumerate(gatherings[:10], 1):
                title = g.get("title", "未知")
                day = g.get("day", "未知日期")
                location = g.get("locationPublic", g.get("location", "未知地点"))
                description = g.get("description", "")
                if description and len(description) > 50:
                    description = description[:47] + "..."
                result += f"{i}. {title}\n   日期：{now.month}月{day}日\n   地点：{location}\n"
                if description:
                    result += f"   简介：{description}\n"
                result += "\n"
                md += (
                    f"**{i}. {title}**\n日期：{now.month}月{day}日\n地点：{location}\n"
                )
                if description:
                    md += f"简介：{description}\n"
                md += "\n"
            await self._send_text(event, result, md)
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("聚会详情")
    async def gathering_detail(self, event: AstrMessageEvent):
        """聚会详情。"""
        gathering_id = _arg(event)
        if not gathering_id:
            await self._send_text(event, "请提供聚会 ID，例如：/聚会详情 12345")
            return
        try:
            data = await self.service.api.get_gathering_detail(gathering_id)
            gathering = data.get("gathering") or data.get("data") or {}
            if not gathering:
                await self._send_text(event, f"未找到聚会 ID '{gathering_id}'")
                return
            result, md = format_gathering_detail(gathering)
            await self._send_text(event, result, md)
        except FileNotFoundError:
            await self._send_text(event, "什么都没有找到呢UwU")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    # ---------------------------------------------------------- Today
    @filter.command("频道动态", alias={"动态探索"})
    async def today_explore(self, event: AstrMessageEvent):
        """Today 探索流。"""
        arg = _arg(event)
        limit = 10
        if arg:
            try:
                limit = max(1, min(int(arg), 24))
            except ValueError:
                pass
        try:
            data = await self.service.api.get_today_explore(limit)
            text = format_today_list(data)
            if not text:
                await self._send_text(event, "暂无频道动态")
                return
            await self._send_text(event, text)
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("动态流", alias={"今日动态"})
    async def today_feed(self, event: AstrMessageEvent):
        """Today 信息流。"""
        args = _arg(event).split()
        date = None
        limit = 10
        for token in args:
            if token.isdigit():
                limit = max(1, min(int(token), 20))
            elif "-" in token:
                date = token
        try:
            data = await self.service.api.get_today_feed(limit=limit, date=date)
            title = f"动态流{('（日期 ' + date + '）') if date else ''}"
            text = format_today_list(data)
            if not text:
                await self._send_text(event, "暂无动态")
                return
            await self._send_text(event, f"{title}\n\n{text}", f"**{title}**\n\n{text}")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("动态详情")
    async def today_detail(self, event: AstrMessageEvent):
        """Today 详情。"""
        today_id = _arg(event)
        if not today_id:
            await self._send_text(event, "请提供动态 ID，例如：/动态详情 xxx")
            return
        try:
            data = await self.service.api.get_today_detail(today_id)
            text = format_today_single(data)
            if not text:
                await self._send_text(
                    event, "该动态不存在或已被下架（可能开启了禁止截图保护）"
                )
                return
            await self._send_text(event, text)
        except FileNotFoundError:
            await self._send_text(event, f"没有找到该动态（{today_id}）")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("话题动态")
    async def today_topics(self, event: AstrMessageEvent):
        """话题下的动态。"""
        args = _arg(event).split()
        if not args:
            await self._send_text(
                event, "请提供话题 ID 或话题名，例如：/话题动态 #兽聚"
            )
            return
        topic = args[0].lstrip("#")
        limit = 10
        if len(args) > 1 and args[1].isdigit():
            limit = max(1, min(int(args[1]), 30))
        try:
            data = await self.service.api.get_today_topics(topic, limit)
            text = format_today_list(data)
            if not text:
                await self._send_text(event, f"话题 '{topic}' 暂无动态")
                return
            await self._send_text(event, text)
        except FileNotFoundError:
            await self._send_text(event, f"没有找到话题 '{topic}'")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("聚会动态")
    async def today_gatherings(self, event: AstrMessageEvent):
        """聚会关联的动态。"""
        args = _arg(event).split()
        if not args:
            await self._send_text(event, "请提供聚会 ID，例如：/聚会动态 12345")
            return
        gathering_id = args[0]
        limit = 10
        if len(args) > 1 and args[1].isdigit():
            limit = max(1, min(int(args[1]), 30))
        try:
            data = await self.service.api.get_today_gatherings(gathering_id, limit)
            text = format_today_list(data)
            if not text:
                await self._send_text(event, f"聚会 '{gathering_id}' 暂无动态")
                return
            await self._send_text(event, text)
        except FileNotFoundError:
            await self._send_text(event, f"没有找到聚会 '{gathering_id}'")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("用户动态", alias={"动态主页"})
    async def today_user(self, event: AstrMessageEvent):
        """用户动态时间线。"""
        args = _arg(event).split()
        if not args:
            await self._send_text(event, "请提供用户名，例如：/用户动态 xj123")
            return
        user = args[0]
        limit = 10
        if len(args) > 1 and args[1].isdigit():
            limit = max(1, min(int(args[1]), 24))
        try:
            data = await self.service.api.get_today_user_timeline(user, limit)
            text = format_today_list(data)
            if not text:
                await self._send_text(event, f"用户 '{user}' 暂无动态")
                return
            await self._send_text(event, text)
        except FileNotFoundError:
            await self._send_text(event, f"没有找到用户 '{user}'")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("今日发布")
    async def today_current(self, event: AstrMessageEvent):
        """某用户今天的动态。"""
        user = _arg(event)
        if not user:
            await self._send_text(event, "请提供用户名，例如：/今日发布 xj123")
            return
        try:
            data = await self.service.api.get_today_user_current(user)
            text = format_today_single(data)
            if not text:
                await self._send_text(event, f"用户 '{user}' 今天还没有发布动态")
                return
            await self._send_text(
                event,
                f"用户 {user} 今天的动态：\n{text}",
                f"**用户 {user} 今天的动态**\n\n{text}",
            )
        except FileNotFoundError:
            await self._send_text(event, f"没有找到用户 '{user}'")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    # ---------------------------------------------------------- 发现
    @filter.command("搜索建议")
    async def search_suggest(self, event: AstrMessageEvent):
        """搜索自动补全建议。"""
        q = _arg(event)
        if not q or len(q) < 2:
            await self._send_text(event, "请提供至少 2 个字符，例如：/搜索建议 狐")
            return
        try:
            data = await self.service.api.get_search_suggestions(q)
            await self._send_text(event, f"「{q}」{format_suggestions(data)}")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    @filter.command("点赞状态")
    async def like_status(self, event: AstrMessageEvent):
        """查询点赞状态。"""
        username = _arg(event)
        if not username:
            await self._send_text(event, "请提供用户名，例如：/点赞状态 xj123")
            return
        try:
            data = await self.service.api.get_user_like_status(username)
            body = format_like_status(data)
            await self._send_text(
                event,
                f"用户 {username} 点赞状态：\n{body}",
                f"**用户 {username} 点赞状态**\n\n{body}",
            )
        except FileNotFoundError:
            await self._send_text(event, f"没有找到用户 '{username}'")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"获取失败：{e}")

    # ---------------------------------------------------------- 帮助
    @filter.command("兽频道帮助", alias={"ftvhelp"})
    async def ftvhelp(self, event: AstrMessageEvent):
        """生成并发送帮助图片。"""
        content = self.service.settings.help_text or _default_help_text()
        path = await asyncio.to_thread(render.generate_help_image, content)
        if not path:
            await self._send_text(event, content)
            return
        from astrbot.api.message_components import Image

        await event.send(event.chain_result([Image.fromFileSystem(path)]))

    # ---------------------------------------------------------- 内部
    async def _send_cards(self, event: AstrMessageEvent, items: list, title: str):
        """发送档案卡片（单张渲染卡片，多张渲染拼图）。"""
        if not items:
            return
        if len(items) == 1:
            img = await render.generate_user_info_image(items[0])
        else:
            img = await render.generate_users_collage(items)
        if img is not None:
            await self._send_image_text(event, img, "", "", alt=title)
            return
        # 图片失败回落文本
        plain = f"{title}\n\n"
        md = f"**{title}**\n\n"
        for i, item in enumerate(items, 1):
            plain += f"{i}. {format_user_info(item)}\n\n"
            md += f"{i}. {format_user_info_md(item)}\n\n"
        await self._send_text(event, plain, md)

    async def _send_images(self, event: AstrMessageEvent, images: list, text: str):
        """发送多张图片（同一条消息）。"""
        urls = [image_host.host_image(img) for img in images]
        if all(urls):
            blocks = [
                image_host.image_markdown(
                    url, "统计图", *image_host.md_display_size(img.width, img.height)
                )
                for url, img in zip(urls, images)
            ]
            body = "\n\n".join(blocks)
            content = f"{text}\n\n{body}" if text else body
            chain = event.make_result().message(content)
            chain.use_markdown(True)
            await event.send(chain)
            return
        from astrbot.api.message_components import Image

        comps = [Plain(text)] if text else []
        for img in images:
            buf = BytesIO()
            img.save(buf, format="PNG")
            comps.append(Image.fromBytes(buf.getvalue()))
        await event.send(event.chain_result(comps))


def _default_help_text() -> str:
    """默认帮助文案（内置，供未配置时使用）。"""
    return (
        "CYX-bot 兽频道菜单 · 指令一览\n"
        "1. /热门 — 获取热门推荐档案\n"
        "2. /随机 — 获取随机推荐档案\n"
        "3. /物种 — 按物种搜索档案，如：/物种 狼\n"
        "4. /兽搜索 — 关键词搜索档案\n"
        "5. /兽档案 — 查看档案资料\n"
        "6. /崽崽 — 查看档案崽崽列表\n"
        "7. /学校 — 按学校搜索档案\n"
        "8. /聚会统计 — 查看年度聚会统计\n"
        "9. /本月聚会 — 查看本月聚会列表\n"
        "10. /聚会详情 — 查看聚会完整信息\n"
        "11. /热门地区 — 热门地区统计\n"
        "12. /物种列表 — 物种统计\n"
        "13. /动态流 — 时间线动态（可补日期）\n"
        "14. /兽频道帮助 — 查看帮助图片\n"
    )
