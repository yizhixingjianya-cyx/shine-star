from astrbot.api import logger
from astrbot.core.message.components import Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..admin_api import AdminAPI
from ..config import PluginConfig
from ..data import QQAdminDB
from ..utils import display_user, extract_image_url, get_ats, get_nickname


class NormalHandle:
    def __init__(self, config: PluginConfig, db: QQAdminDB):
        self.cfg = config
        self.db = db

    async def set_group_ban(
        self,
        event: AiocqhttpMessageEvent,
        ban_time: int | None = None,
        target_id: str | int = "",
    ):
        api = AdminAPI(event)
        group_config = self.db.get_group_snapshot(event.get_group_id())
        # 命令不带秒数时，@ 出来的文本会被当成参数传进来，这里统一按“未指定时长”处理
        if not isinstance(ban_time, int):
            try:
                ban_time = int(str(ban_time).strip())
            except ValueError:
                ban_time = self.cfg.get_ban_time_with_range(
                    group_config.get("random_ban_time"), 60
                )
        tids = [target_id] if target_id else get_ats(event)
        results = []
        for tid in tids:
            target_display = await display_user(event, tid)
            try:
                await api.set_group_ban(user_id=tid, duration=ban_time)
                results.append(f"用户[{target_display}]已被禁言{ban_time}秒")
            except Exception as e:
                logger.error(f"禁言用户{tid}失败：{e}")
                results.append(f"用户[{target_display}]禁言失败：{e}")
        event.stop_event()
        return "\n".join(results) if results else "未指定要禁言的用户"

    async def set_group_whole_ban(self, event: AiocqhttpMessageEvent, enable: bool):
        await AdminAPI(event).set_group_whole_ban(enable=enable)
        return "已开启全体禁言" if enable else "已关闭全体禁言"

    async def set_group_card(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str | int = "",
        target_card: str | int = "",
    ):
        api = AdminAPI(event)
        tids = ([target_id] if target_id else get_ats(event)) or [event.get_sender_id()]
        results = []
        for tid in tids:
            target_name = await get_nickname(event, user_id=tid)
            results.append(
                f"已修改{target_name}的群昵称为【{target_card}】"
                if target_card
                else f"已清除{target_name}的群昵称"
            )
            await api.set_group_card(user_id=tid, card=str(target_card))
        return "\n".join(results) if results else "未指定要设置群昵称的用户"

    async def set_group_special_title(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str | int = "",
        special_title: str | int = "",
    ):
        api = AdminAPI(event)
        tids = ([target_id] if target_id else get_ats(event)) or [event.get_sender_id()]
        results = []
        for tid in tids:
            target_name = await get_nickname(event, user_id=tid)
            results.append(
                f"已修改{target_name}的头衔为【{special_title}】"
                if special_title
                else f"已清除{target_name}的头衔"
            )
            await api.set_group_special_title(user_id=tid, special_title=special_title)
        return "\n".join(results) if results else "未指定要设置头衔的用户"

    async def set_group_kick(
        self, event: AiocqhttpMessageEvent, target_id: str | int = ""
    ):
        api = AdminAPI(event)
        tids = [target_id] if target_id else get_ats(event)
        results = []
        for tid in tids:
            target_name = await get_nickname(event, user_id=tid)
            await api.set_group_kick(user_id=tid, reject_add_request=False)
            results.append(f"已将【{tid}-{target_name}】踢出本群")
        return "\n".join(results) if results else "未指定要踢出的用户"

    async def set_group_block(
        self, event: AiocqhttpMessageEvent, target_id: str | int = ""
    ):
        api = AdminAPI(event)
        tids = [target_id] if target_id else get_ats(event)
        results = []
        for tid in tids:
            target_name = await get_nickname(event, user_id=tid)
            await api.set_group_kick(user_id=tid, reject_add_request=True)
            results.append(f"已将【{tid}-{target_name}】踢出本群并拉黑!")
        return "\n".join(results) if results else "未指定要拉黑的用户"

    async def set_group_admin(self, event: AiocqhttpMessageEvent, enable: bool):
        api = AdminAPI(event)
        results = []
        for tid in get_ats(event):
            target_name = await get_nickname(event, user_id=tid)
            await api.set_group_admin(user_id=tid, enable=enable)
            msg = (
                f"{target_name}已被设为管理员"
                if enable
                else f"{target_name}的管理员身份已被取消"
            )
            results.append(msg)
        return "\n".join(results) if results else "未指定要操作的用户"

    async def set_essence_msg(
        self,
        event: AiocqhttpMessageEvent,
        enable: bool,
        message_id: str | int = "",
    ):
        if not message_id:
            chain = event.get_messages()
            first_seg = chain[0] if chain else None
            if not isinstance(first_seg, Reply):
                return "未指定要设置精华的消息"
            message_id = first_seg.id
        api = AdminAPI(event)
        if enable:
            await api.set_essence_msg(message_id=message_id)
            return "已设为精华消息"
        else:
            await api.delete_essence_msg(message_id=message_id)
            return "已取消精华消息"

    async def get_essence_msg_list(self, event: AiocqhttpMessageEvent):
        """查看群精华"""
        essence_data = await AdminAPI(event).get_essence_msg_list()
        if not essence_data:
            return "没有群精华消息"
        return f"{essence_data}"

    async def set_group_portrait(
        self, event: AiocqhttpMessageEvent, image_url: str | None = None
    ):
        image_url = image_url or extract_image_url(chain=event.get_messages())
        if not image_url:
            return "未获取到新头像"
        await AdminAPI(event).set_group_portrait(file=image_url)
        return "群头像已更新"

    async def set_group_name(
        self, event: AiocqhttpMessageEvent, group_name: str | int | None = None
    ):
        if not group_name:
            return "未输入新群名"
        await AdminAPI(event).set_group_name(group_name=str(group_name))
        return f"本群群名更新为：{group_name}"
