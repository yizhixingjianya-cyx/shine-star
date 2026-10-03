"""QQ 官方机器人原生消息能力核心实现（原生 Markdown / 按钮 / Ark / 主动推送）。

这些能力只有 QQ 官方机器人（``qq_official`` / ``qq_official_webhook`` 适配器）才有：
AstrBot 的通用 ``send_message_to_user`` 只会发送纯文本与富媒体，无法指定
``msg_type=2`` 的原生 Markdown、消息按钮（keyboard）以及 Ark 卡片。

本模块只依赖标准库（``botpy`` 为可选依赖，仅在识别平台异常类型时使用），
不导入任何 AstrBot 符号，因此可以脱离 AstrBot 运行时独立联调与测试。
AstrBot 侧的 LLM 工具封装见 ``main.py``。
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from typing import Any

# QQ 官方机器人适配器名称，见 astrbot/core/platform/sources/qqofficial*
QQ_PLATFORM_NAMES = ("qq_official", "qq_official_webhook")

# 目标场景，对应 QQ 官方机器人四个发送接口
SCENE_AUTO = "auto"
SCENE_GROUP = "group"  # QQ 群聊      POST /v2/groups/{group_openid}/messages
SCENE_C2C = "c2c"  # QQ 单聊      POST /v2/users/{openid}/messages
SCENE_CHANNEL = "channel"  # 文字子频道    POST /channels/{channel_id}/messages
SCENE_DM = "dm"  # 频道私信      POST /dms/{guild_id}/messages
SCENES = (SCENE_AUTO, SCENE_GROUP, SCENE_C2C, SCENE_CHANNEL, SCENE_DM)

# QQ 开放平台在机器人没有原生 Markdown 权限时返回的错误文案，
# 与 qqofficial_message_event.py 中的 MARKDOWN_NOT_ALLOWED_ERROR 保持一致。
MARKDOWN_NOT_ALLOWED_ERROR = "不允许发送原生 markdown"
# 主动消息超频（主动消息在单聊/群聊场景每月有额度限制）。
MSG_LIMIT_EXCEEDED_ERROR = "msg limit exceed"

MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_ARK = 3

# 自定义按钮限制，见 https://bot.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html
MAX_BUTTON_ROWS = 5
MAX_BUTTONS_PER_ROW = 5

_SCENE_LABELS = {
    SCENE_GROUP: "QQ 群聊",
    SCENE_C2C: "QQ 单聊",
    SCENE_CHANNEL: "文字子频道",
    SCENE_DM: "频道私信",
}

# Markdown 降级为纯文本时使用的替换规则（按顺序应用）
_MARKDOWN_TO_PLAIN_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"!\[[^\]]*\]\(([^)\s]+)[^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)"), r"\1 (\2)"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"__\*\*(.+?)\*\*__"), r"\1"),
    (re.compile(r"\*\*\*(.+?)\*\*\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"^\s*>\s?", re.MULTILINE), ""),
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), "· "),
    (re.compile(r"^\s*-{3,}\s*$", re.MULTILINE), "———"),
    (re.compile(r"\n{3,}"), "\n\n"),
)


class QQNativeError(Exception):
    """QQ 原生消息发送过程中的可预期错误（会原样回传给大模型）。"""


def scene_label(scene: str) -> str:
    """返回场景的中文名称，未知场景原样返回。

    Args:
        scene: 场景标识，例如 ``group``。

    Returns:
        场景的中文描述。
    """
    return _SCENE_LABELS.get(scene, scene)


def markdown_to_plain(text: str) -> str:
    """把 Markdown 粗略转换为纯文本，用于平台拒绝原生 Markdown 时的降级。

    Args:
        text: Markdown 原文。

    Returns:
        去掉大部分标记的纯文本。
    """
    plain = text or ""
    for pattern, replacement in _MARKDOWN_TO_PLAIN_RULES:
        plain = pattern.sub(replacement, plain)
    return plain.strip()


def build_markdown(
    content: str | None = None,
    template_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 QQ ``markdown`` 字段。

    自定义 Markdown 与 Markdown 模版可以同时存在，但至少要提供一个。

    Args:
        content: 自定义 Markdown 文本。
        template_id: 在 QQ 开放平台申请的 Markdown 模版 ID。
        params: 模版变量映射，值为字符串或字符串列表。

    Returns:
        QQ 发送接口的 ``markdown`` 对象。

    Raises:
        QQNativeError: 三个参数都为空时抛出。
    """
    markdown: dict[str, Any] = {}
    if template_id:
        markdown["custom_template_id"] = str(template_id)
        markdown["params"] = [
            {
                "key": str(key),
                "values": [str(item) for item in value]
                if isinstance(value, (list, tuple))
                else [str(value)],
            }
            for key, value in (params or {}).items()
        ]
    if content:
        markdown["content"] = str(content)
    if not markdown:
        raise QQNativeError(
            "需要提供 content（自定义 Markdown）或 template_id（Markdown 模版）之一。",
        )
    return markdown


def build_ark(
    template_id: Any, kv: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """构造 QQ ``ark`` 字段。

    Args:
        template_id: Ark 模版 ID（整数）。
        kv: Ark 模版变量列表，每项形如 ``{"key": "k", "value": "v"}``。

    Returns:
        QQ 发送接口的 ``ark`` 对象。

    Raises:
        QQNativeError: ``template_id`` 不是合法整数时抛出。
    """
    try:
        ark_template_id = int(template_id)
    except (TypeError, ValueError) as exc:
        raise QQNativeError(
            f"ark 的 template_id 必须是整数，收到：{template_id!r}"
        ) from exc

    ark_kv: list[dict[str, Any]] = []
    for item in kv or []:
        if not isinstance(item, Mapping):
            raise QQNativeError(
                "ark 的 kv 每一项都必须是对象，形如 {'key': 'k', 'value': 'v'}。"
            )
        key = str(item.get("key") or "").strip()
        if not key:
            raise QQNativeError("ark 的 kv 每一项都必须包含 key。")
        ark_kv.append({"key": key, "value": str(item.get("value") or "")})
    return {"template_id": ark_template_id, "kv": ark_kv}


def _to_int(value: Any, default: int) -> int:
    """把任意输入安全转换为整数。

    Args:
        value: 待转换的值。
        default: 转换失败时使用的默认值。

    Returns:
        转换后的整数。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_button(button: Mapping[str, Any], index: int) -> dict[str, Any]:
    """把大模型给出的扁平按钮描述补全成 QQ 要求的 button 对象。

    Args:
        button: 形如 ``{"label": "打卡", "data": "checkin"}`` 的描述。
        index: 按钮序号，用于生成缺省的唯一按钮 ID。

    Returns:
        QQ 要求的 button 对象。

    Raises:
        QQNativeError: 按钮缺少文本或描述不是对象时抛出。
    """
    if not isinstance(button, Mapping):
        raise QQNativeError(f"第 {index + 1} 个按钮必须是对象。")

    label = str(button.get("label") or button.get("text") or "").strip()
    if not label:
        raise QQNativeError(f"第 {index + 1} 个按钮缺少 label（按钮文字）。")

    # type: 0 跳转按钮（data 为链接）、1 回调按钮、2 指令按钮；默认指令按钮。
    action_type = _to_int(button.get("type"), 2)
    # permission.type: 0 指定用户、1 仅管理者、2 所有人、3 指定身份组（仅频道）。
    permission_type = _to_int(button.get("permission"), 2)
    permission: dict[str, Any] = {"type": permission_type}
    user_ids = button.get("specify_user_ids")
    if isinstance(user_ids, Sequence) and not isinstance(user_ids, (str, bytes)):
        permission["specify_user_ids"] = [str(item) for item in user_ids]
    role_ids = button.get("specify_role_ids")
    if isinstance(role_ids, Sequence) and not isinstance(role_ids, (str, bytes)):
        permission["specify_role_ids"] = [str(item) for item in role_ids]

    return {
        "id": str(button.get("id") or f"btn_{index + 1}"),
        "render_data": {
            "label": label,
            "visited_label": str(button.get("visited_label") or label),
            "style": _to_int(button.get("style"), 0),
        },
        "action": {
            "type": action_type,
            "permission": permission,
            "data": str(button.get("data") or label),
            "unsupport_tips": str(
                button.get("unsupport_tips") or "当前客户端不支持该操作"
            ),
        },
    }


def build_keyboard(
    keyboard_id: str | None = None,
    buttons: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """构造 QQ ``keyboard`` 字段。

    支持两种形式：按钮模版（``{"id": ...}``）与自定义按钮（``{"content": {"rows": ...}}``）。

    Args:
        keyboard_id: 在 QQ 开放平台申请到的按钮模版 ID。
        buttons: 扁平按钮列表，通过 ``row`` 指定行号（从 0 开始，最多 5 行、每行 5 个）。

    Returns:
        QQ 发送接口的 ``keyboard`` 对象；两者都未提供时返回 ``None``。

    Raises:
        QQNativeError: 按钮数量、行号越界或按钮内容非法时抛出。
    """
    if keyboard_id:
        if buttons:
            raise QQNativeError(
                "keyboard_id（按钮模版）与 buttons（自定义按钮）只能二选一。"
            )
        return {"id": str(keyboard_id)}
    if not buttons:
        return None

    rows: dict[int, list[dict[str, Any]]] = {}
    for index, button in enumerate(buttons):
        if not isinstance(button, Mapping):
            raise QQNativeError(f"第 {index + 1} 个按钮必须是对象。")
        row_index = _to_int(button.get("row"), 0)
        if not 0 <= row_index < MAX_BUTTON_ROWS:
            raise QQNativeError(
                f"按钮行号必须在 0 ~ {MAX_BUTTON_ROWS - 1} 之间，收到：{row_index}"
            )
        row = rows.setdefault(row_index, [])
        if len(row) >= MAX_BUTTONS_PER_ROW:
            raise QQNativeError(
                f"第 {row_index + 1} 行最多放 {MAX_BUTTONS_PER_ROW} 个按钮。"
            )
        row.append(_normalize_button(button, index))

    return {"content": {"rows": [{"buttons": rows[key]} for key in sorted(rows)]}}


def split_session(session_str: str) -> tuple[str, str, str]:
    """拆分 ``platform_id:message_type:session_id`` 形式的三段式会话。

    Args:
        session_str: 会话字符串。

    Returns:
        平台 ID、消息类型值、会话 ID 三元组。

    Raises:
        QQNativeError: 字符串不是三段式时抛出。
    """
    parts = (session_str or "").split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise QQNativeError(
            f"非法的会话标识：{session_str!r}，应形如 platform_id:message_type:session_id",
        )
    platform_id, message_type, session_id = (part.strip() for part in parts)
    return platform_id, message_type, session_id


def normalize_target_id(session_id: str, message_type: str) -> str:
    """归一化发送目标 ID，与 AstrBot QQ 适配器的行为保持一致。

    频道的会话 ID 历史上可能是 ``guild_id_channel_id`` 的复合形式，
    适配器会取最后一段作为 ``channel_id``。

    Args:
        session_id: 三段式会话中的会话 ID。
        message_type: 消息类型值，例如 ``GroupMessage``。

    Returns:
        直接用于 QQ 发送接口的目标 ID。
    """
    target_id = session_id or ""
    if message_type == "GroupMessage":
        target_id = target_id.rsplit("_", 1)[-1]
    return target_id


def _should_retry_without_msg_id(exc: BaseException) -> bool:
    """判断平台异常是否属于「回复消息不可用，应改用主动推送接口」。

    Args:
        exc: 发送过程中捕获到的异常。

    Returns:
        是否应当去掉 ``msg_id`` 重试。
    """
    try:
        import botpy.errors as botpy_errors

        retry_types: tuple[type[BaseException], ...] = (
            botpy_errors.ForbiddenError,
            botpy_errors.MethodNotAllowedError,
            botpy_errors.NotFoundError,
            botpy_errors.SequenceNumberError,
            botpy_errors.ServerError,
        )
        if isinstance(exc, retry_types):
            return True
    except Exception:  # noqa: BLE001 - botpy 缺失时退化为按错误文案判断
        pass

    text = str(exc).lower()
    keywords = (
        "msg_id",
        "message id",
        "not found",
        "forbidden",
        "method not allowed",
        "sequence",
        "越权",
        "无效",
    )
    return any(keyword in text for keyword in keywords)


class QQNativeSender:
    """把 AstrBot 会话映射到 QQ 官方机器人原生发送接口。

    Args:
        api: qq-botpy 的 ``BotAPI`` 实例（即 ``platform.get_client().api``）。
        markdown_fallback_to_plain: 平台以「不允许发送原生 markdown」拒绝时，
            是否自动降级为纯文本重试。
    """

    def __init__(self, api: Any, *, markdown_fallback_to_plain: bool = True) -> None:
        self.api = api
        self.markdown_fallback_to_plain = markdown_fallback_to_plain

    async def _dispatch(
        self, scene: str, target_id: str, payload: dict[str, Any]
    ) -> Any:
        """按场景调用对应的 QQ 发送接口。

        Args:
            scene: 目标场景。
            target_id: 目标 ID（group_openid / openid / channel_id / guild_id）。
            payload: 接口请求体。

        Returns:
            qq-botpy 的响应对象。

        Raises:
            QQNativeError: 场景不受支持时抛出。
        """
        if scene == SCENE_GROUP:
            return await self.api.post_group_message(group_openid=target_id, **payload)
        if scene == SCENE_C2C:
            return await self.api.post_c2c_message(openid=target_id, **payload)
        if scene == SCENE_CHANNEL:
            return await self.api.post_message(channel_id=target_id, **payload)
        if scene == SCENE_DM:
            return await self.api.post_dms(guild_id=target_id, **payload)
        raise QQNativeError(f"不支持的发送场景：{scene}")

    async def _dispatch_with_resilience(
        self,
        scene: str,
        target_id: str,
        payload: dict[str, Any],
        *,
        text_fallback: dict[str, Any] | None = None,
    ) -> Any:
        """带「被动转主动」与「Markdown 降级」的发送。

        Args:
            scene: 目标场景。
            target_id: 目标 ID。
            payload: 首选请求体。
            text_fallback: 平台拒绝原生 Markdown 时改用的纯文本请求体。

        Returns:
            qq-botpy 的响应对象。

        Raises:
            QQNativeError: 所有重试方式都失败时抛出。
        """
        try:
            return await self._dispatch(scene, target_id, payload)
        except Exception as exc:  # noqa: BLE001 - 统一转成面向大模型的错误文案
            error_text = str(exc)
            if payload.get("msg_id") and _should_retry_without_msg_id(exc):
                # 回复消息超时（5 分钟）或 msg_id 失效时，退化为主动推送。
                proactive_payload = {
                    key: value for key, value in payload.items() if key != "msg_id"
                }
                try:
                    return await self._dispatch(scene, target_id, proactive_payload)
                except Exception as retry_exc:  # noqa: BLE001
                    error_text = str(retry_exc)

            if (
                text_fallback is not None
                and self.markdown_fallback_to_plain
                and MARKDOWN_NOT_ALLOWED_ERROR in error_text
            ):
                try:
                    return await self._dispatch(scene, target_id, text_fallback)
                except Exception as fallback_exc:  # noqa: BLE001
                    error_text = str(fallback_exc)

            raise QQNativeError(error_text) from exc

    async def send_markdown(
        self,
        *,
        scene: str,
        target_id: str,
        content: str | None = None,
        template_id: str | None = None,
        params: Mapping[str, Any] | None = None,
        keyboard_id: str | None = None,
        buttons: Sequence[Mapping[str, Any]] | None = None,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        event_id: str | None = None,
        plain_fallback: str | None = None,
    ) -> str:
        """发送 QQ 原生 Markdown 消息（可挂载按钮）。

        Args:
            scene: 目标场景。
            target_id: 目标 ID。
            content: 自定义 Markdown 文本。
            template_id: Markdown 模版 ID。
            params: Markdown 模版变量。
            keyboard_id: 按钮模版 ID。
            buttons: 自定义按钮列表。
            msg_id: 被动回复所用的消息 ID；为空即主动推送。
            msg_seq: 回复序号，主动推送时缺省随机，避免重复发送被平台拒绝。
            event_id: 交互事件 ID，可用于按钮回调等场景的被动回复。
            plain_fallback: 降级用的纯文本；为空时由 ``content`` 自动转换。

        Returns:
            面向大模型的中文结果描述。

        Raises:
            QQNativeError: 参数非法或平台返回错误时抛出。
        """
        markdown = build_markdown(
            content=content, template_id=template_id, params=params
        )
        keyboard = build_keyboard(keyboard_id=keyboard_id, buttons=buttons)

        payload: dict[str, Any] = {"markdown": markdown}
        if scene in (SCENE_GROUP, SCENE_C2C):
            payload["msg_type"] = MSG_TYPE_MARKDOWN
        if keyboard:
            payload["keyboard"] = keyboard
        self._apply_reply_fields(
            payload, scene=scene, msg_id=msg_id, msg_seq=msg_seq, event_id=event_id
        )

        text_fallback: dict[str, Any] | None = None
        fallback_text = plain_fallback or markdown_to_plain(str(content or ""))
        if fallback_text:
            text_fallback = {"content": fallback_text}
            if scene in (SCENE_GROUP, SCENE_C2C):
                text_fallback["msg_type"] = MSG_TYPE_TEXT
            self._apply_reply_fields(
                text_fallback,
                scene=scene,
                msg_id=msg_id,
                msg_seq=msg_seq,
                event_id=event_id,
            )

        ret = await self._dispatch_with_resilience(
            scene,
            target_id,
            payload,
            text_fallback=text_fallback,
        )
        mode = "主动推送" if not msg_id else "被动回复"
        return (
            f"已以{mode}方式发送 QQ 原生 Markdown 到{scene_label(scene)}（{target_id}）。"
            f"{self._describe_message(ret, keyboard)}"
        )

    async def send_ark(
        self,
        *,
        scene: str,
        target_id: str,
        template_id: Any,
        kv: Sequence[Mapping[str, Any]] | None = None,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        event_id: str | None = None,
    ) -> str:
        """发送 QQ Ark 模版卡片消息。

        Args:
            scene: 目标场景。
            target_id: 目标 ID。
            template_id: Ark 模版 ID。
            kv: Ark 模版变量列表。
            msg_id: 被动回复所用的消息 ID；为空即主动推送。
            msg_seq: 回复序号。
            event_id: 交互事件 ID。

        Returns:
            面向大模型的中文结果描述。
        """
        ark = build_ark(template_id, kv)
        payload: dict[str, Any] = {"ark": ark}
        if scene in (SCENE_GROUP, SCENE_C2C):
            payload["msg_type"] = MSG_TYPE_ARK
        self._apply_reply_fields(
            payload, scene=scene, msg_id=msg_id, msg_seq=msg_seq, event_id=event_id
        )

        ret = await self._dispatch_with_resilience(scene, target_id, payload)
        mode = "主动推送" if not msg_id else "被动回复"
        return (
            f"已以{mode}方式发送 QQ Ark 卡片到{scene_label(scene)}（{target_id}），"
            f"模版 ID：{ark['template_id']}。{self._describe_message(ret, None)}"
        )

    @staticmethod
    def _apply_reply_fields(
        payload: dict[str, Any],
        *,
        scene: str,
        msg_id: str | None,
        msg_seq: int | None,
        event_id: str | None,
    ) -> None:
        """按场景写入被动回复相关字段。

        Args:
            payload: 待补全的请求体。
            scene: 目标场景。
            msg_id: 被动回复的消息 ID。
            msg_seq: 回复序号。
            event_id: 交互事件 ID。

        Returns:
            None.
        """
        if msg_id:
            payload["msg_id"] = str(msg_id)
            if scene in (SCENE_GROUP, SCENE_C2C):
                payload["msg_seq"] = (
                    int(msg_seq) if msg_seq else random.randint(1, 10000)
                )
        elif event_id:
            payload["event_id"] = str(event_id)
        elif scene in (SCENE_GROUP, SCENE_C2C):
            # 主动推送同样需要 msg_seq，用于规避平台侧的重复消息判定。
            payload["msg_seq"] = int(msg_seq) if msg_seq else random.randint(1, 10000)

    @staticmethod
    def _describe_message(ret: Any, keyboard: dict[str, Any] | None) -> str:
        """把 qq-botpy 的响应整理成简短的补充说明。

        Args:
            ret: qq-botpy 响应对象或字典。
            keyboard: 本次发送使用的 keyboard 字段。

        Returns:
            补充说明文本，可能为空串。
        """
        message_id = (
            ret.get("id") if isinstance(ret, dict) else getattr(ret, "id", None)
        )
        parts = []
        if message_id:
            parts.append(f"消息 ID：{message_id}")
        if keyboard:
            parts.append("已附带按钮" if "id" in keyboard else "已附带自定义按钮")
        return (" ".join(parts) + "。") if parts else ""
