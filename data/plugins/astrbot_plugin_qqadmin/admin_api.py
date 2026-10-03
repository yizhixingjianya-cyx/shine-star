"""群管 API 适配层：aiocqhttp 转调 OneBot 接口，QQ 官方机器人调用官方 API

官方接口规范见 https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import botpy.errors
from botpy.http import Route

from astrbot.api.event import AstrMessageEvent
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

# QQ 官方机器人平台（WebSocket / Webhook），群与成员 ID 均为 openid 字符串
OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
# 已适配的平台：OneBot v11 与 QQ 官方机器人
SUPPORTED_PLATFORMS = {"aiocqhttp", *OFFICIAL_PLATFORMS}
SUPPORTED_ADAPTER_TYPES = (
    PlatformAdapterType.AIOCQHTTP
    | PlatformAdapterType.QQOFFICIAL
    | PlatformAdapterType.QQOFFICIAL_WEBHOOK
)
# 官方禁言接口的到期时间为 RFC3339 格式，采用北京时间
OFFICIAL_TZ = timezone(timedelta(hours=8))
# 官方限制：最长禁言 30 天
OFFICIAL_MAX_BAN_SECONDS = 2592000


class OfficialUnsupportedError(NotImplementedError):
    """官方机器人未提供对应接口"""


class OfficialAPIError(RuntimeError):
    """官方接口调用失败"""


def is_official(event: AstrMessageEvent) -> bool:
    """判断事件是否来自 QQ 官方机器人平台"""
    return event.platform_meta.name in OFFICIAL_PLATFORMS


def is_supported(event: AstrMessageEvent) -> bool:
    """判断事件所在平台是否已适配"""
    return event.platform_meta.name in SUPPORTED_PLATFORMS


def official_users(event: AstrMessageEvent) -> dict[str, dict]:
    """取出官方群消息里的 openid -> 成员信息

    官方群消息的 author 与 mentions 都是带 member_role 的 User 对象，
    因此群角色可以不依赖内邀开放的群成员接口；mention 标记该成员是否被 @ 到。

    Args:
        event: 群聊消息事件。

    Returns:
        openid 到 {"role": 群角色, "nickname": 昵称, "mention": 是否被 @} 的映射。
    """
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    data = getattr(raw, "raw_data", None)
    if not isinstance(data, dict):
        return {}
    users: dict[str, dict] = {}
    for user, mention in [
        (data.get("author"), False),
        *((item, True) for item in data.get("mentions") or []),
    ]:
        if not isinstance(user, dict) or not (
            user.get("member_openid") or user.get("id")
        ):
            continue
        info = {
            "role": str(user.get("member_role") or ""),
            "nickname": str(user.get("username") or ""),
            "mention": mention,
        }
        for key in ("member_openid", "id"):
            if user.get(key):
                users[str(user[key])] = info
    return users


async def official_request(
    bot, group_id: str, method: str, path: str, **payload
) -> dict:
    """请求 QQ 官方群接口，可用于没有事件的后台任务

    Args:
        bot: 官方机器人客户端。
        group_id: 群 openid。
        method: HTTP 方法。
        path: /v2/groups/{group_openid} 之后的接口路径。
        **payload: 请求体。

    Returns:
        接口返回的 JSON 字典，无返回内容时为空字典。

    Raises:
        OfficialAPIError: 接口调用失败。
    """
    route = Route(method, f"/v2/groups/{group_id}{path}")
    kwargs = {"json": payload} if payload else {}
    # 官方偶发 5xx（"处理失败，请稍后重试"），写操作按幂等重试一次
    for attempt in range(2):
        try:
            result = await bot.api._http.request(route, **kwargs)
        except botpy.errors.ServerError as e:
            if attempt:
                raise OfficialAPIError(f"官方机器人接口调用失败：{e}") from e
            await asyncio.sleep(1)
        except Exception as e:
            raise OfficialAPIError(f"官方机器人接口调用失败：{e}") from e
        else:
            return result if isinstance(result, dict) else {}
    return {}


class AdminAPI:
    """群管 API 门面，按平台分发到 OneBot 或 QQ 官方接口

    Args:
        event: 群聊消息事件。
    """

    def __init__(self, event: AstrMessageEvent):
        self.event = event
        self.bot = event.bot
        self.official = is_official(event)

    async def _onebot(self, name: str, **kwargs):
        """转调 OneBot 接口，群号与用户号统一转成整数

        Args:
            name: OneBot 接口名。
            **kwargs: 接口参数，group_id 与 user_id 会被转成整数。

        Returns:
            OneBot 接口的返回值。
        """
        for key in ("group_id", "user_id"):
            if kwargs.get(key) is not None:
                kwargs[key] = int(kwargs[key])
        return await getattr(self.bot, name)(**kwargs)

    async def _request(self, method: str, path: str, **payload) -> dict:
        """请求 QQ 官方 API

        Args:
            method: HTTP 方法。
            path: /v2/groups/{group_openid} 之后的接口路径。
            **payload: 请求体。

        Returns:
            接口返回的 JSON 字典，无返回内容时为空字典。

        Raises:
            OfficialAPIError: 接口调用失败。
        """
        return await official_request(
            self.bot, self.event.get_group_id(), method, path, **payload
        )

    async def set_group_ban(self, user_id, duration=0, group_id=None):
        """禁言群成员，duration 为 0 表示解除禁言"""
        if not self.official:
            await self._onebot(
                "set_group_ban",
                group_id=group_id or self.event.get_group_id(),
                user_id=user_id,
                duration=duration,
            )
            return
        seconds = int(duration or 0)
        member = {
            "op": "add" if seconds > 0 else "del",
            "member_openid": str(user_id),
            "mute_expire_at": "",
        }
        if seconds > 0:
            # 与 OneBot 的“禁言秒数”不同，官方接口要的是绝对到期时间：
            # 本地只能精确到秒，且请求往返本身也要耗时，
            # 因此向上取整到整秒再多给 1 秒，避免实际禁言短于设置值
            expire_at = datetime.now(OFFICIAL_TZ) + timedelta(
                seconds=min(seconds + 1, OFFICIAL_MAX_BAN_SECONDS)
            )
            if expire_at.microsecond:
                expire_at += timedelta(seconds=1)
            member["mute_expire_at"] = expire_at.replace(microsecond=0).isoformat()
        await self._request("POST", "/restrict_chat_setting", members=[member])

    async def set_group_kick(self, user_id, reject_add_request=False, group_id=None):
        """将群成员移出群聊，reject_add_request 为真时同时加入群黑名单"""
        if not self.official:
            await self._onebot(
                "set_group_kick",
                group_id=group_id or self.event.get_group_id(),
                user_id=user_id,
                reject_add_request=reject_add_request,
            )
            return
        await self._request(
            "POST",
            "/batch_remove_members",
            member_openids=[str(user_id)],
            add_to_member_blacklist=bool(reject_add_request),
        )

    async def delete_msg(self, message_id):
        """撤回群消息"""
        if not self.official:
            await self._onebot("delete_msg", message_id=int(message_id))
            return
        await self._request("DELETE", f"/messages/{message_id}")

    async def get_group_member_info(self, user_id, group_id=None, no_cache=False):
        """获取群成员信息，返回 OneBot 风格的资料字典"""
        if not self.official:
            return await self._onebot(
                "get_group_member_info",
                group_id=group_id or self.event.get_group_id(),
                user_id=user_id,
                no_cache=no_cache,
            )
        # 官方群消息自带成员角色，优先使用，无需内邀开放的群成员接口
        user = official_users(self.event).get(str(user_id))
        if user and user["role"]:
            return {
                "user_id": str(user_id),
                "nickname": user["nickname"],
                "role": user["role"],
                "level": 0,
            }
        if str(user_id) == str(self.event.get_self_id()):
            data = await self._request("GET", "/bot_state")
        else:
            data = await self._request("GET", f"/members/{user_id}")
        return {
            "user_id": str(user_id),
            "nickname": data.get("username", ""),
            "role": data.get("member_role", "unknown"),
            "level": 0,
        }

    async def get_group_member_list(self, group_id=None):
        """获取群成员列表"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持获取群成员列表")
        return await self._onebot(
            "get_group_member_list", group_id=group_id or self.event.get_group_id()
        )

    async def get_stranger_info(self, user_id):
        """获取用户资料"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持查询用户资料")
        return await self._onebot("get_stranger_info", user_id=user_id)

    async def get_group_msg_history(self, **payloads):
        """拉取群历史消息"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持批量撤回消息")
        for key in ("group_id", "user_id"):
            if payloads.get(key) is not None:
                payloads[key] = int(payloads[key])
        return await self.bot.api.call_action("get_group_msg_history", **payloads)

    async def set_group_whole_ban(self, enable=True, group_id=None):
        """开启/关闭群全员禁言"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持全员禁言")
        await self._onebot(
            "set_group_whole_ban",
            group_id=group_id or self.event.get_group_id(),
            enable=enable,
        )

    async def set_group_card(self, user_id, card="", group_id=None):
        """设置群名片"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持修改群昵称")
        await self._onebot(
            "set_group_card",
            group_id=group_id or self.event.get_group_id(),
            user_id=user_id,
            card=str(card),
        )

    async def set_group_special_title(
        self, user_id, special_title="", duration=-1, group_id=None
    ):
        """设置群头衔"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持设置群头衔")
        await self._onebot(
            "set_group_special_title",
            group_id=group_id or self.event.get_group_id(),
            user_id=user_id,
            special_title=str(special_title),
            duration=duration,
        )

    async def set_group_admin(self, user_id, enable=True, group_id=None):
        """设置/取消群管理员"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持设置管理员")
        await self._onebot(
            "set_group_admin",
            group_id=group_id or self.event.get_group_id(),
            user_id=user_id,
            enable=enable,
        )

    async def set_essence_msg(self, message_id):
        """设置群精华消息"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群精华")
        await self._onebot("set_essence_msg", message_id=int(message_id))

    async def delete_essence_msg(self, message_id):
        """移除群精华消息"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群精华")
        await self._onebot("delete_essence_msg", message_id=int(message_id))

    async def get_essence_msg_list(self, group_id=None):
        """获取群精华消息列表"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群精华")
        return await self._onebot(
            "get_essence_msg_list", group_id=group_id or self.event.get_group_id()
        )

    async def set_group_portrait(self, file, group_id=None):
        """设置群头像"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持修改群头像")
        await self._onebot(
            "set_group_portrait",
            group_id=group_id or self.event.get_group_id(),
            file=file,
        )

    async def set_group_name(self, group_name, group_id=None):
        """设置群名称"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持修改群名")
        await self._onebot(
            "set_group_name",
            group_id=group_id or self.event.get_group_id(),
            group_name=str(group_name),
        )

    async def send_group_notice(self, content, image="", group_id=None):
        """发布群公告"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群公告")
        await self._onebot(
            "_send_group_notice",
            group_id=group_id or self.event.get_group_id(),
            content=content,
            image=str(image),
        )

    async def get_group_notice(self, group_id=None):
        """获取群公告"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群公告")
        return await self._onebot(
            "_get_group_notice", group_id=group_id or self.event.get_group_id()
        )

    async def get_group_root_files(self, group_id=None):
        """获取群根目录文件列表"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群文件")
        return await self._onebot(
            "get_group_root_files", group_id=group_id or self.event.get_group_id()
        )

    async def get_group_files_by_folder(self, folder_id, group_id=None):
        """获取群文件夹内的文件列表"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群文件")
        return await self._onebot(
            "get_group_files_by_folder",
            group_id=group_id or self.event.get_group_id(),
            folder_id=folder_id,
        )

    async def create_group_file_folder(self, folder_name, parent_id="/", group_id=None):
        """创建群文件夹"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群文件")
        await self._onebot(
            "create_group_file_folder",
            group_id=group_id or self.event.get_group_id(),
            folder_name=folder_name,
            parent_id=parent_id,
        )

    async def upload_group_file(self, file, name, folder_id=None, group_id=None):
        """上传群文件"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群文件")
        await self._onebot(
            "upload_group_file",
            group_id=group_id or self.event.get_group_id(),
            file=file,
            name=name,
            folder_id=folder_id,
        )

    async def delete_group_file(self, file_id, group_id=None):
        """删除群文件"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群文件")
        await self._onebot(
            "delete_group_file",
            group_id=group_id or self.event.get_group_id(),
            file_id=file_id,
        )

    async def delete_group_folder(self, folder_id, group_id=None):
        """删除群文件夹"""
        if self.official:
            raise OfficialUnsupportedError("官方机器人暂不支持群文件")
        await self._onebot(
            "delete_group_folder",
            group_id=group_id or self.event.get_group_id(),
            folder_id=folder_id,
        )
