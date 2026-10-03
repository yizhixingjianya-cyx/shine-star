import os
import re
from datetime import datetime
from pathlib import Path

from aiohttp import ClientSession

from astrbot import logger
from astrbot.core.message.components import At, BaseMessageComponent, Image, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .admin_api import AdminAPI, is_official, official_users

# 官方群消息会把 @ 目标留在文本里，形如 <@openid> / <@!openid>
AT_PATTERN = re.compile(r"<@!?([0-9A-Za-z]{16,})>")

# 各群独立的 openid -> 昵称 缓存：官方平台的 openid 对人不可读，
# 发言/@ 时顺手记下来，后续展示直接取用（进程内缓存，重启后随发言重建）
_NAME_CACHE: dict[str, dict[str, str]] = {}


def remember_names(group_id: str, names: dict[str, str]) -> None:
    """记录群内 openid 对应的昵称"""
    cache = _NAME_CACHE.setdefault(str(group_id), {})
    cache.update({str(uid): name for uid, name in names.items() if name})


def recall_name(group_id: str, user_id: str | int) -> str:
    """取缓存的昵称，没有则返回空串"""
    return _NAME_CACHE.get(str(group_id), {}).get(str(user_id), "")


async def get_nickname(event: AiocqhttpMessageEvent, user_id: int | str) -> str:
    """获取指定群友的群昵称或 Q 名，群接口失败/空结果自动降级到陌生人资料与本地缓存"""
    api = AdminAPI(event)
    group_id = event.get_group_id()
    info = {}

    # 在群里就先试群资料，任何异常或空结果都跳过
    if api.official or group_id.isdigit():
        try:
            info = await api.get_group_member_info(user_id=user_id) or {}
        except Exception:
            pass

    # 群资料没拿到就降级到陌生人资料
    if not info:
        try:
            info = await api.get_stranger_info(user_id=user_id) or {}
        except Exception:
            pass

    # 依次取群名片、QQ 昵称、通用 nick，兜底本地缓存与 UID
    name = info.get("card") or info.get("nickname") or info.get("nick")
    if name:
        remember_names(group_id, {str(user_id): str(name)})
        return str(name)
    return recall_name(group_id, user_id) or str(user_id)


async def display_user(event: AiocqhttpMessageEvent, user_id: int | str) -> str:
    """展示用名称：官方平台为「昵称(openid)」，昵称未知或 OneBot 平台时只给号码"""
    uid = str(user_id)
    name = await get_nickname(event, uid) if is_official(event) else uid
    return uid if not name or name == uid else f"{name}({uid})"


def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
    """获取被at者们的id列表"""
    self_id = event.get_self_id()
    ids = [
        str(seg.qq)
        for seg in event.get_messages()
        if (isinstance(seg, At) and str(seg.qq) != self_id)
    ]
    if not is_official(event):
        return ids
    # 官方群消息不会把其他成员转成 At 组件，@ 目标在 mentions 与文本里
    candidates = [uid for uid, info in official_users(event).items() if info["mention"]]
    for uid in (*candidates, *AT_PATTERN.findall(event.message_str)):
        if uid != self_id and uid not in ids:
            ids.append(uid)
    return ids


def get_replyer_id(event: AiocqhttpMessageEvent) -> str | None:
    """获取被引用消息者的id"""
    for seg in event.get_messages():
        if isinstance(seg, Reply):
            return str(seg.sender_id)


def get_reply_message_str(event: AiocqhttpMessageEvent) -> str | None:
    """
    获取被引用的消息解析后的纯文本消息字符串。
    """
    return next(
        (
            seg.message_str
            for seg in event.message_obj.message
            if isinstance(seg, Reply)
        ),
        "",
    )


def format_time(timestamp):
    """格式化时间戳"""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


async def download_file(url: str, save_path: Path) -> Path | None:
    """下载文件并保存到本地"""
    url = url.replace("https://", "http://")
    try:
        async with ClientSession() as client:
            response = await client.get(url)
            file = await response.read()

            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            with open(save_path, "wb") as img_file:
                img_file.write(file)

            logger.info(f"文件已保存: {save_path}")
            return save_path
    except Exception as e:
        logger.error(f"文件下载并保存失败: {e}")
        return None


def extract_image_url(chain: list[BaseMessageComponent]) -> str | None:
    """从消息链中提取图片URL"""
    for seg in chain:
        if isinstance(seg, Image):
            return seg.url
        elif isinstance(seg, Reply) and seg.chain:
            for reply_seg in seg.chain:
                if isinstance(reply_seg, Image):
                    return reply_seg.url
    return None


def parse_bool(mode: str | bool | None, default: bool = False):
    """解析布尔值"""
    mode = str(mode).strip().lower()
    match mode:
        case "开" | "开启" | "启用" | "on" | "true" | "1" | "是" | "真":
            return True
        case "关" | "关闭" | "禁用" | "off" | "false" | "0" | "否" | "假":
            return False
        case _:
            return default
